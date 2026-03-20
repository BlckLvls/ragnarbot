"""Agent loop: the core processing engine."""

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from ragnarbot.agent.background import BackgroundProcessManager
from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.compactor import Compactor
from ragnarbot.agent.context import ContextBuilder
from ragnarbot.agent.fallback import FallbackState
from ragnarbot.agent.memory_flush import MemoryFlushManager, MemorySegment
from ragnarbot.agent.subagent import SubagentManager
from ragnarbot.agent.tools.agent_tools import AgentTool
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
from ragnarbot.instance import (
    bind_pending_update_target,
    clear_pending_update,
    load_pending_update,
    pending_update_target,
    record_last_active_chat,
)
from ragnarbot.media.manager import MediaManager
from ragnarbot.providers.base import LLMProvider, LLMResponse
from ragnarbot.providers.lightning import (
    LIGHTNING_COST_NOTE,
    LIGHTNING_UNSUPPORTED_NOTE,
    LIGHTNING_WORKS_NOTE,
    resolve_lightning,
)
from ragnarbot.providers.reasoning import SUPPORTED_REASONING_LEVELS
from ragnarbot.session.manager import SessionManager

if TYPE_CHECKING:
    from ragnarbot.agent.agents_loader import AgentDefinition
    from ragnarbot.config.schema import BrowserConfig, ExecToolConfig, FallbackConfig
    from ragnarbot.cron.service import CronService


@dataclass
class BrowserCallState:
    """Tracks browser state for the currently executing browser tool call."""

    action: str
    before_ids: set[str]
    pre_touched: set[str] = field(default_factory=set)


@dataclass
class RunState:
    """Mutable state for the currently active foreground agent run."""

    session_key: str
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_steering: deque[InboundMessage] = field(default_factory=deque)
    injected_steering: list[dict[str, Any]] = field(default_factory=list)
    active_tool_task: asyncio.Task | None = None
    active_browser_call: BrowserCallState | None = None
    touched_browser_sessions: set[str] = field(default_factory=set)


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
    READ_ONLY_COMMANDS = frozenset({
        "context_info", "context_mode", "lightning", "reasoning", "stop", "trace",
    })
    IMMEDIATE_COMMANDS = READ_ONLY_COMMANDS | frozenset({
        "set_context_mode",
        "set_lightning_mode",
        "set_reasoning_level",
        "set_trace_mode",
        "steering",
        "set_steering_mode",
    })

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
        reasoning_level: str = "medium",
        lightning_mode: bool = False,
        auth_method: str = "api_key",
        heartbeat_interval_m: int = 30,
        fallback_model: str | None = None,
        fallback_config: "FallbackConfig | None" = None,
        provider_factory: "Callable | None" = None,
        trace_mode: bool = False,
        steering_enabled: bool = True,
        browser_config: "BrowserConfig | None" = None,
    ):
        from ragnarbot.config.schema import ExecToolConfig
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
        self.reasoning_level = reasoning_level
        self.lightning_mode = lightning_mode
        self.auth_method = auth_method
        self.trace_mode = trace_mode
        self.steering_enabled = steering_enabled
        self.cache_manager = CacheManager(max_context_tokens=max_context_tokens)

        # Fallback model support
        self._fallback_state = FallbackState.load()
        self._fallback_provider: LLMProvider | None = None
        self._fallback_model: str | None = fallback_model
        self._fallback_config = fallback_config
        self._provider_factory = provider_factory
        self._fb_just_recovered = False
        self._fb_error_notified = False
        self.compactor = Compactor(
            provider=provider,
            cache_manager=self.cache_manager,
            max_context_tokens=max_context_tokens,
            model=self.model,
            chat_fn=self._chat_with_fallback,
        )

        self.context = ContextBuilder(workspace, heartbeat_interval_m=heartbeat_interval_m)
        self.context.model = self.model
        self.sessions = SessionManager(workspace)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self.memory_flush = MemoryFlushManager(
            workspace=workspace,
            sessions=self.sessions,
            chat_fn=self._chat_with_fallback,
            save_session_fn=self._save_session_locked,
        )
        self.tools = ToolRegistry()

        from ragnarbot.agent.tools.browser import BrowserSessionManager
        from ragnarbot.config.schema import BrowserConfig
        self.browser_config = browser_config or BrowserConfig()
        self.browser_manager = BrowserSessionManager(config=self.browser_config)

        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            agents_loader=self.context.agents,
            model=self.model,
            brave_api_key=brave_api_key,
            search_engine=search_engine,
            exec_config=self.exec_config,
            chat_fn=self._chat_with_fallback,
            on_fallback_batch=self._record_fallback_batch,
            browser_manager=self.browser_manager,
            context_builder=self.context,
        )

        self.bg_processes = BackgroundProcessManager(
            bus=bus, workspace=workspace, exec_config=self.exec_config,
        )

        self._running = False
        self._restart_requested = False
        self._processing_task: asyncio.Task | None = None
        self._run_state: RunState | None = None
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
            safety_guard=self.exec_config.safety_guard,
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

        # Agent tools (for sub-agents)
        self.tools.register(AgentTool(manager=self.subagents))

        # Browser tool
        from ragnarbot.agent.tools.browser import BrowserTool
        self.tools.register(BrowserTool(manager=self.browser_manager))

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
        await self.memory_flush.resume_pending_jobs()
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

            # Immediate commands: respond immediately, even during processing
            if command in self.IMMEDIATE_COMMANDS:
                response = await self._handle_command(command, msg)
                if response:
                    if (command != "stop"
                            and self._processing_task
                            and not self._processing_task.done()):
                        response.metadata["keep_typing"] = True
                    await self.bus.publish_outbound(response)
                continue

            if self._processing_task and not self._processing_task.done():
                if self._queue_steering_message(msg):
                    continue
                await self._await_processing_task()

            if msg.channel not in {"system", "cli"} and not command:
                await self.deliver_pending_update_notice(msg.channel, msg.chat_id)

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
                    response = await self._handle_command(command, msg)
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
        state = self._get_run_state(session_key)
        if state is not None:
            return state.stop_event.is_set()
        event = self._stop_events.get(session_key)
        return event is not None and event.is_set()

    def _request_stop(self, session_key: str) -> bool:
        """Returns True if there was something to stop."""
        if (self._processing_task and not self._processing_task.done()
                and self._processing_session_key == session_key):
            state = self._get_run_state(session_key)
            event = state.stop_event if state else self._stop_events.get(session_key)
            if event:
                event.set()
                if state and state.active_tool_task and not state.active_tool_task.done():
                    state.active_tool_task.cancel()
                return True
        return False

    def _get_run_state(self, session_key: str | None = None) -> RunState | None:
        """Return the active run state, optionally scoped to a session."""
        if self._run_state is None:
            return None
        if session_key is None or self._run_state.session_key == session_key:
            return self._run_state
        return None

    def _start_run_state(self, session_key: str) -> RunState:
        """Create a fresh run state for the active session."""
        event = asyncio.Event()
        self._stop_events[session_key] = event
        self._run_state = RunState(session_key=session_key, stop_event=event)
        return self._run_state

    async def _requeue_pending_steering(self, state: RunState) -> None:
        """Push any unconsumed steering messages back onto the bus."""
        while state.pending_steering:
            await self.bus.publish_inbound(state.pending_steering.popleft())

    async def _finish_run_state(self, session_key: str) -> None:
        """Clear the active run state and preserve pending steering as next turns."""
        state = self._get_run_state(session_key)
        if state is not None:
            await self._requeue_pending_steering(state)
            self._run_state = None
        self._stop_events.pop(session_key, None)

    def _queue_steering_message(self, msg: InboundMessage) -> bool:
        """Queue a same-session message as steering during an active run."""
        if not self.steering_enabled:
            return False
        if msg.channel == "system" or msg.metadata.get("command"):
            return False
        state = self._get_run_state(msg.session_key)
        if state is None:
            return False
        state.pending_steering.append(msg)
        logger.info(f"Queued steering message for active run {msg.session_key}")
        return True

    async def _chat_or_stop(
        self, session_key: str, provider: LLMProvider | None = None, **chat_kwargs,
    ):
        """Race provider.chat() against the stop event.

        Returns the LLM response, or None if stopped mid-call.
        """
        provider = provider or self.provider
        event = self._stop_events.get(session_key)
        chat_task = asyncio.create_task(provider.chat(**chat_kwargs))

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

    def _get_or_create_fallback_provider(self) -> LLMProvider | None:
        """Lazily create the fallback provider on first use."""
        if self._fallback_provider is not None:
            return self._fallback_provider
        if not self._provider_factory or not self._fallback_model:
            return None
        try:
            auth_method = (
                self._fallback_config.auth_method if self._fallback_config else "api_key"
            )
            self._fallback_provider = self._provider_factory(
                self._fallback_model, auth_method,
            )
            return self._fallback_provider
        except Exception as e:
            logger.error(f"Failed to create fallback provider: {e}")
            return None

    async def _chat_with_fallback(
        self,
        session_key: str | None,
        *,
        notify_channel: str | None = None,
        notify_chat_id: str | None = None,
        **chat_kwargs,
    ) -> tuple[LLMResponse | None, bool, str | None]:
        """Call primary LLM with automatic fallback on error.

        Returns (response, used_fallback, primary_error).
        response is None only if stopped by user.
        used_fallback is True when the response came from the fallback provider.
        primary_error is the error string when primary failed (None otherwise).
        """

        state = self._fallback_state
        has_fallback = self._fallback_model is not None

        # Decide which provider to try first
        probe_interval = (
            self._fallback_config.recovery_probe_interval if self._fallback_config else 60
        )
        use_primary = not state.fallback_mode or state.should_probe_primary(probe_interval)
        response = None
        primary_error = None
        chat_kwargs.setdefault("reasoning_level", self.reasoning_level)
        chat_kwargs.setdefault("lightning_mode", self.lightning_mode)
        chat_kwargs.setdefault("model", self.model)

        if use_primary:
            if state.fallback_mode:
                state.mark_primary_probed()
                logger.info("Probing primary provider for recovery...")

            if session_key:
                response = await self._chat_or_stop(
                    session_key, provider=self.provider, **chat_kwargs,
                )
            else:
                response = await self.provider.chat(**chat_kwargs)

            if response is None:
                return None, False, None  # stopped by user

            if response.finish_reason != "error":
                was_fallback = state.record_primary_success()
                if was_fallback:
                    logger.info("Primary provider recovered — exiting fallback mode")
                    self._fb_just_recovered = True
                    state.save()
                return response, False, None

            # Primary failed
            primary_error = response.content or "Unknown error"
            logger.warning(f"Primary provider error: {primary_error[:200]}")

            if not has_fallback:
                return response, False, primary_error  # no fallback configured

            # Send error immediately — user sees it while fallback is processing
            if notify_channel and not self._fb_error_notified and not state.fallback_mode:
                self._fb_error_notified = True
                short_err = primary_error[:200]
                await self.bus.publish_outbound(OutboundMessage(
                    channel=notify_channel, chat_id=notify_chat_id,
                    content=f"\u26a0\ufe0f {short_err}\nRetrying with fallback...",
                    metadata={"intermediate": True},
                ))

        # Fallback path
        fb_provider = self._get_or_create_fallback_provider()
        if fb_provider is None:
            logger.error("Failed to create fallback provider")
            if response is not None:
                return response, False, primary_error
            return (
                LLMResponse(content="No provider available.", finish_reason="error"),
                False, None,
            )

        logger.info(f"Using fallback provider: {self._fallback_model}")

        chat_kwargs["model"] = self._fallback_model
        if session_key:
            fb_response = await self._chat_or_stop(
                session_key, provider=fb_provider, **chat_kwargs,
            )
        else:
            fb_response = await fb_provider.chat(**chat_kwargs)

        if fb_response is None:
            return None, True, primary_error  # stopped by user

        if fb_response.finish_reason == "error":
            logger.error(f"Fallback provider also failed: {fb_response.content}")
            return fb_response, True, primary_error

        return fb_response, True, primary_error

    async def _record_fallback_batch(
        self, used_fallback: bool, channel: str | None = None, chat_id: str | None = None,
    ) -> None:
        """Record batch-level fallback accounting. Call once per user interaction."""
        if not used_fallback:
            return
        state = self._fallback_state
        threshold = (
            self._fallback_config.consecutive_failures_threshold
            if self._fallback_config else 3
        )
        just_entered = state.record_primary_failure(threshold)
        state.save()
        if just_entered and channel:
            logger.warning(
                f"Entering fallback mode after {threshold} consecutive failures"
            )
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel, chat_id=chat_id,
                content=(
                    f"\U0001f504 Switching to fallback mode "
                    f"({self._fallback_model}). Normal mode will be "
                    f"restored automatically when the primary model "
                    f"becomes available."
                ),
                metadata={"intermediate": True},
            ))

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

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Return a shared lock for coordinated session saves."""
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    async def _save_session_locked(self, session) -> None:
        """Persist a session under a per-session lock."""
        async with self._get_session_lock(session.key):
            self.sessions.save(session)

    @staticmethod
    def _message_extras(message: dict[str, Any]) -> dict[str, Any]:
        """Extract session-persisted extras from an LLM-format message."""
        extras: dict[str, Any] = {}
        if "tool_calls" in message:
            extras["tool_calls"] = message["tool_calls"]
        if "tool_call_id" in message:
            extras["tool_call_id"] = message["tool_call_id"]
        if "name" in message:
            extras["name"] = message["name"]
        return extras

    @staticmethod
    def _message_user_meta(msg: InboundMessage, steering: bool = False) -> dict[str, Any]:
        """Build session metadata for a user message."""
        meta = {
            key: msg.metadata[key]
            for key in ("message_id", "reply_to", "forwarded_from")
            if key in msg.metadata
        }
        if steering:
            meta["type"] = "steering"
        return meta

    async def _prepare_inbound_message(
        self,
        session,
        msg: InboundMessage,
        *,
        include_timestamp: bool,
        steering: bool = False,
    ) -> dict[str, Any]:
        """Convert an inbound message into LLM-ready content plus save metadata."""
        from datetime import datetime as _dt

        from ragnarbot.session.manager import _build_message_prefix

        media_refs: list[dict[str, str]] = []
        if self.media_manager:
            for att in msg.attachments:
                if att.type == "photo" and att.data:
                    ext = _ext_from_mime(att.mime_type)
                    filename = await self.media_manager.save_photo(
                        session.key, att.data, ext,
                    )
                    media_refs.append({"type": "photo", "filename": filename})

        reply_to = msg.metadata.get("reply_to")
        if reply_to and isinstance(reply_to, dict) and self.media_manager:
            photo_data = reply_to.pop("photo_data", None)
            photo_mime = reply_to.pop("photo_mime", None)
            if photo_data:
                ext = _ext_from_mime(photo_mime)
                filename = await self.media_manager.save_photo(
                    session.key, photo_data, ext,
                )
                media_refs.append({"type": "photo", "filename": filename})
                reply_to["has_photo"] = True

        if self.media_manager:
            photo_paths = [
                str(self.media_manager.get_photo_path(session.key, ref["filename"]))
                for ref in media_refs
                if ref["type"] == "photo"
            ]
            if photo_paths:
                markers = "\n".join(f"[photo saved: {p}]" for p in photo_paths)
                msg.content = f"{msg.content}\n{markers}" if msg.content else markers

        current_meta: dict[str, Any] = {}
        if include_timestamp:
            current_meta["timestamp"] = _dt.now().isoformat()
        if steering:
            current_meta["type"] = "steering"
        for key in ("reply_to", "forwarded_from"):
            if key in msg.metadata:
                current_meta[key] = msg.metadata[key]
        prefix = _build_message_prefix(current_meta, include_timestamp=include_timestamp)
        prefixed_content = prefix + msg.content if prefix else msg.content

        system_note = msg.metadata.get("system_note")
        if system_note:
            prefixed_content += f"\n\n{system_note}"

        return {
            "prefixed_content": prefixed_content,
            "media_refs": media_refs,
            "media": msg.media if msg.media else None,
            "raw_msg": msg,
        }

    async def _inject_pending_steering(
        self,
        session_key: str,
        session,
        messages: list[dict[str, Any]],
    ) -> bool:
        """Append queued steering messages before the next LLM call."""
        state = self._get_run_state(session_key)
        if state is None or not state.pending_steering:
            return False

        injected = 0
        while state.pending_steering:
            steering_msg = state.pending_steering.popleft()
            prepared = await self._prepare_inbound_message(
                session, steering_msg, include_timestamp=True, steering=True,
            )
            messages.append(self.context.build_user_message(
                content=prepared["prefixed_content"],
                media=prepared["media"],
                media_refs=prepared["media_refs"] or None,
                session_key=session.key,
            ))
            state.injected_steering.append(prepared)
            injected += 1

        logger.info(f"Injected {injected} steering message(s) into active run {session_key}")
        return True

    def _begin_browser_call(
        self, state: RunState, arguments: dict[str, Any],
    ) -> BrowserCallState:
        """Capture browser session state before executing a browser tool call."""
        action = str(arguments.get("action", ""))
        call_state = BrowserCallState(
            action=action,
            before_ids=self.browser_manager.current_session_ids(),
            pre_touched=self.browser_manager.estimate_touched_sessions(
                action, session_id=arguments.get("session_id"),
            ),
        )
        state.active_browser_call = call_state
        return call_state

    def _record_browser_touch(self, state: RunState, call_state: BrowserCallState) -> None:
        """Record browser sessions touched by a browser tool call."""
        touched = set(call_state.pre_touched)
        after_ids = self.browser_manager.current_session_ids()
        if call_state.action in {"open", "connect"}:
            touched.update(after_ids - call_state.before_ids)
        elif call_state.action == "close_all":
            touched.update(call_state.before_ids)
        state.touched_browser_sessions.update(touched)

    def _finalize_active_browser_call(self, state: RunState) -> None:
        """Flush browser touch tracking for the active browser call, if any."""
        call_state = state.active_browser_call
        if call_state is None:
            return
        self._record_browser_touch(state, call_state)
        state.active_browser_call = None

    async def _execute_tool_with_tracking(
        self,
        session_key: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | list[dict[str, Any]]:
        """Execute a tool while tracking foreground cancellation state."""
        state = self._get_run_state(session_key)
        if state is None:
            return await self.tools.execute(tool_name, arguments)

        if tool_name == "browser":
            self._begin_browser_call(state, arguments)

        tool_task = asyncio.create_task(self.tools.execute(tool_name, arguments))
        state.active_tool_task = tool_task
        try:
            return await tool_task
        finally:
            if tool_name == "browser":
                self._finalize_active_browser_call(state)
            if state.active_tool_task is tool_task:
                state.active_tool_task = None

    async def _cleanup_stopped_run(self, session_key: str) -> None:
        """Cleanup foreground resources after a stop request."""
        state = self._get_run_state(session_key)
        if state is None:
            return

        if state.active_tool_task and not state.active_tool_task.done():
            state.active_tool_task.cancel()
            try:
                await state.active_tool_task
            except (asyncio.CancelledError, Exception):
                pass

        self._finalize_active_browser_call(state)

        for browser_session_id in list(state.touched_browser_sessions):
            if browser_session_id in self.browser_manager.current_session_ids():
                await self.browser_manager.close(browser_session_id)
        state.touched_browser_sessions.clear()

    def _save_batch_messages(
        self,
        session,
        messages: list[dict[str, Any]],
        new_start: int,
        batch_data: list[dict[str, Any]],
        steering_data: list[dict[str, Any]],
    ) -> None:
        """Persist the current user turn, including injected steering."""
        steering_idx = 0
        for idx, message in enumerate(messages[new_start:]):
            extras = self._message_extras(message)
            if message["role"] == "user":
                if idx < len(batch_data):
                    prepared = batch_data[idx]
                    raw = prepared["raw_msg"]
                    if prepared["media_refs"]:
                        extras["media_refs"] = prepared["media_refs"]
                    session.add_message(
                        "user",
                        raw.content,
                        msg_metadata=self._message_user_meta(raw),
                        **extras,
                    )
                else:
                    prepared = steering_data[steering_idx]
                    steering_idx += 1
                    raw = prepared["raw_msg"]
                    if prepared["media_refs"]:
                        extras["media_refs"] = prepared["media_refs"]
                    session.add_message(
                        "user",
                        raw.content,
                        msg_metadata=self._message_user_meta(raw, steering=True),
                        **extras,
                    )
            else:
                session.add_message(message["role"], message.get("content"), **extras)

    def _save_system_messages(
        self,
        session,
        messages: list[dict[str, Any]],
        new_start: int,
        msg: InboundMessage,
        steering_data: list[dict[str, Any]],
    ) -> None:
        """Persist a system-triggered turn plus any injected steering."""
        steering_idx = 0
        for idx, message in enumerate(messages[new_start:]):
            extras = self._message_extras(message)
            if message["role"] == "user":
                if idx == 0:
                    session.add_message(
                        "user",
                        f"[System: {msg.sender_id}] {msg.content}",
                        msg_metadata=self._message_user_meta(msg),
                        **extras,
                    )
                    continue

                prepared = steering_data[steering_idx]
                steering_idx += 1
                raw = prepared["raw_msg"]
                if prepared["media_refs"]:
                    extras["media_refs"] = prepared["media_refs"]
                session.add_message(
                    "user",
                    raw.content,
                    msg_metadata=self._message_user_meta(raw, steering=True),
                    **extras,
                )
            else:
                session.add_message(message["role"], message.get("content"), **extras)

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
        self._start_run_state(session_key)

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
            await self._finish_run_state(session_key)

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
                response = await self._handle_command(command, msg)
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
        msg = batch[0]

        if msg.channel != "cli":
            self.last_active_chat = (msg.channel, msg.chat_id)
            record_last_active_chat(msg.channel, msg.chat_id)

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
        agent_tool = self.tools.get("agent")
        if isinstance(agent_tool, AgentTool):
            agent_tool.set_context(msg.channel, msg.chat_id)

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
        batch_data: list[dict] = []
        for m in batch:
            batch_data.append(await self._prepare_inbound_message(
                session, m, include_timestamp=(m is batch[0]),
            ))

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
        batch_used_fallback = False  # track once per user message batch
        start_memory_jobs = False
        self._fb_error_notified = False  # reset per batch

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
                    messages, new_start, memory_segment = await self.compactor.compact(
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
                    if memory_segment is not None:
                        created_jobs = self.memory_flush.enqueue_segment(
                            session, memory_segment,
                        )
                        start_memory_jobs = start_memory_jobs or bool(created_jobs)
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
                response, used_fallback, primary_error = await self._chat_with_fallback(
                    session_key,
                    notify_channel=msg.channel,
                    notify_chat_id=msg.chat_id,
                    messages=api_messages,
                    tools=tools_defs,
                )

                # LLM call cancelled by stop
                if response is None:
                    logger.info(f"LLM call cancelled by stop for {session_key}")
                    stopped = True
                    break

                # Recovery notification — primary came back after fallback mode
                if not used_fallback and self._fb_just_recovered:
                    self._fb_just_recovered = False
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content=(
                            "\u2705 Primary model is back online. "
                            "Switched back to normal mode."
                        ),
                        metadata={"intermediate": True},
                    ))

                if used_fallback:
                    batch_used_fallback = True

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
                    messages = self.context.add_assistant_message(
                        messages, response.content, tool_call_dicts
                    )

                    # Truncated — return error for each tool call
                    if response.finish_reason == "length":
                        logger.warning(
                            "Response truncated (finish_reason=length), "
                            "rejecting tool calls"
                        )
                        for tc in response.tool_calls:
                            messages = self.context.add_tool_result(
                                messages, tc.id, tc.name,
                                "Error: response was cut off (max_tokens "
                                "limit reached) and this tool call was "
                                "incomplete. Your output must fit within "
                                "the token limit. Split large content "
                                "into smaller calls.",
                            )
                        continue

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

                        if self.trace_mode:
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=self._format_trace(
                                    tool_call.name, tool_call.arguments,
                                ),
                                metadata={"intermediate": True, "raw_html": True},
                            ))

                        try:
                            result = await self._execute_tool_with_tracking(
                                session_key, tool_call.name, tool_call.arguments,
                            )
                        except asyncio.CancelledError:
                            if not self._is_stopped(session_key):
                                raise
                            logger.info(
                                f"Tool execution cancelled by stop for {session_key}"
                            )
                            stopped = True
                            messages = self.context.add_tool_result(
                                messages, tool_call.id, tool_call.name, "[Stopped by user]",
                            )
                            for remaining in response.tool_calls[idx + 1:]:
                                messages = self.context.add_tool_result(
                                    messages, remaining.id, remaining.name, "[Stopped by user]",
                                )
                            break
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )

                    if stopped:
                        break
                    await self._inject_pending_steering(session_key, session, messages)
                else:
                    final_content = response.content
                    break
        finally:
            # Persist cache metadata even if tool execution throws, so
            # should_flush() sees the correct created_at on the next turn.
            await self._save_session_locked(session)

        if stopped:
            await self._cleanup_stopped_run(session_key)

        # Fallback accounting — count once per user message batch
        outbound_content = final_content
        await self._record_fallback_batch(batch_used_fallback, msg.channel, msg.chat_id)

        # Tag only the outbound message in normal mode (not in fallback mode).
        # Don't save the tag to session — prevents LLM from duplicating it.
        if batch_used_fallback and outbound_content and not self._fallback_state.fallback_mode:
            outbound_content += f"\n\n_\u26a1 fallback: {self._fallback_model}_"

        if not stopped:
            messages.append({"role": "assistant", "content": final_content or ""})

        state = self._get_run_state(session_key)
        steering_data = state.injected_steering if state is not None else []
        self._save_batch_messages(session, messages, new_start, batch_data, steering_data)
        await self._save_session_locked(session)

        if start_memory_jobs:
            await self.memory_flush.start_session_jobs(session.key)

        if stopped or not outbound_content:
            return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=outbound_content
        )

    async def _handle_command(self, command: str, msg: InboundMessage) -> OutboundMessage | None:
        """Dispatch a channel command without calling the LLM."""
        if command == "new_chat":
            return await self._handle_new_chat(msg)
        if command == "stop":
            return self._handle_stop(msg)
        if command == "context_mode":
            return self._handle_context_mode(msg)
        if command == "set_context_mode":
            return self._handle_set_context_mode(msg)
        if command == "context_info":
            return self._handle_context_info(msg)
        if command == "reasoning":
            return self._handle_reasoning(msg)
        if command == "set_reasoning_level":
            return self._handle_set_reasoning_level(msg)
        if command == "lightning":
            return self._handle_lightning(msg)
        if command == "set_lightning_mode":
            return self._handle_set_lightning_mode(msg)
        if command == "trace":
            return self._handle_trace(msg)
        if command == "set_trace_mode":
            return self._handle_set_trace_mode(msg)
        if command == "steering":
            return self._handle_steering(msg)
        if command == "set_steering_mode":
            return self._handle_set_steering_mode(msg)
        logger.warning(f"Unknown command: {command}")
        return None

    async def _handle_new_chat(self, msg: InboundMessage) -> OutboundMessage:
        """Create a new chat session and return a confirmation message."""
        old_session = self.sessions.get_or_create(msg.session_key)
        tail_segment = self._build_new_chat_memory_segment(old_session)
        if tail_segment is not None:
            self.memory_flush.enqueue_segment(old_session, tail_segment)
            await self._save_session_locked(old_session)

        session = self.sessions.create_new(msg.session_key)

        if msg.channel == "telegram":
            session.metadata["user_data"] = {
                "user_id": msg.metadata.get("user_id"),
                "username": msg.metadata.get("username"),
                "first_name": msg.metadata.get("first_name"),
                "last_name": msg.metadata.get("last_name"),
            }
            await self._save_session_locked(session)

        if tail_segment is not None:
            await self.memory_flush.start_session_jobs(old_session.key)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=f"✨ <b>New chat started</b>\n\n🤖 Model: <code>{self.model}</code>",
            metadata={"raw_html": True},
        )

    def _build_new_chat_memory_segment(self, session) -> MemorySegment | None:
        """Return the unflushed tail that should be persisted when starting a new chat."""
        last_idx = self.compactor._find_last_compaction_idx(session.messages)
        start_idx = 0 if last_idx is None else last_idx + 1
        end_idx = len(session.messages)
        if end_idx <= start_idx:
            return None
        return MemorySegment(
            start_idx=start_idx,
            end_idx=end_idx,
            trigger="new_chat",
            flush_type="extra_hard",
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

    def _handle_trace(self, msg: InboundMessage) -> OutboundMessage:
        """Show current trace mode with toggle button."""
        enabled = self.trace_mode
        status = "Enabled" if enabled else "Disabled"
        toggle = "off" if enabled else "on"
        btn_text = "Disable" if enabled else "Enable"
        text = f"🔍 <b>Trace Mode</b>\n\nCurrent: {status}"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={
                "raw_html": True,
                "inline_keyboard": [[
                    {"text": btn_text, "callback_data": f"trace_mode:{toggle}"},
                ]],
            },
        )

    def _handle_set_trace_mode(self, msg: InboundMessage) -> OutboundMessage | None:
        """Toggle trace mode (from callback query)."""
        value = msg.metadata.get("trace_mode")
        if value not in ("on", "off"):
            return None

        self.trace_mode = value == "on"
        from ragnarbot.config.loader import load_config, save_config
        config = load_config()
        config.agents.defaults.trace_mode = self.trace_mode
        save_config(config)

        status = "Enabled" if self.trace_mode else "Disabled"
        text = f"✅ Trace mode: {status}"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={
                "raw_html": True,
                "edit_message_id": msg.metadata.get("callback_message_id"),
            },
        )

    def _get_lightning_resolution(self):
        """Resolve Lightning Mode for the current runtime model/auth pair."""
        return resolve_lightning(self.model, self.auth_method, self.lightning_mode)

    def _build_lightning_message(self) -> str:
        """Render the Lightning Mode toggle panel."""
        resolution = self._get_lightning_resolution()
        status = "Enabled" if self.lightning_mode else "Disabled"
        lines = [
            "⚡ <b>Lightning Mode</b>",
            "",
            f"Current: {status}",
            "",
            LIGHTNING_WORKS_NOTE,
            LIGHTNING_COST_NOTE,
        ]
        if not resolution.supported:
            lines.extend(["", LIGHTNING_UNSUPPORTED_NOTE])
        return "\n".join(lines)

    def _build_lightning_keyboard(self) -> list[list[dict[str, str]]]:
        """Build the Lightning Mode toggle keyboard."""
        enabled = self.lightning_mode
        toggle = "off" if enabled else "on"
        btn_text = "Disable" if enabled else "Enable"
        return [[{"text": btn_text, "callback_data": f"lightning_mode:{toggle}"}]]

    def _handle_lightning(self, msg: InboundMessage) -> OutboundMessage:
        """Show Lightning Mode with a single enable/disable button."""
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self._build_lightning_message(),
            metadata={
                "raw_html": True,
                "inline_keyboard": self._build_lightning_keyboard(),
            },
        )

    def _handle_set_lightning_mode(self, msg: InboundMessage) -> OutboundMessage | None:
        """Toggle Lightning Mode (from callback query)."""
        value = msg.metadata.get("lightning_mode")
        if value not in ("on", "off"):
            return None

        self.lightning_mode = value == "on"
        from ragnarbot.config.loader import load_config, save_config
        config = load_config()
        config.agents.defaults.lightning_mode = self.lightning_mode
        save_config(config)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self._build_lightning_message(),
            metadata={
                "raw_html": True,
                "edit_message_id": msg.metadata.get("callback_message_id"),
                "inline_keyboard": self._build_lightning_keyboard(),
            },
        )

    def _build_reasoning_message(self) -> str:
        """Render the reasoning-level control panel."""
        selected = self.reasoning_level.replace("_", " ").title()
        return f"🧠 <b>Reasoning</b>\n\nSelected: <b>{selected}</b>"

    def _build_reasoning_keyboard(self) -> list[list[dict[str, str]]]:
        """Build the reasoning picker keyboard in a single vertical column."""
        keyboard: list[list[dict[str, str]]] = []
        for level in SUPPORTED_REASONING_LEVELS:
            label = level.title()
            if level == self.reasoning_level:
                label = f"✓ {label}"
            keyboard.append([{
                "text": label,
                "callback_data": f"reasoning_level:{level}",
            }])
        return keyboard

    def _handle_reasoning(self, msg: InboundMessage) -> OutboundMessage:
        """Show reasoning-level picker."""
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self._build_reasoning_message(),
            metadata={
                "raw_html": True,
                "inline_keyboard": self._build_reasoning_keyboard(),
            },
        )

    def _handle_set_reasoning_level(self, msg: InboundMessage) -> OutboundMessage | None:
        """Persist a new reasoning level and refresh the panel."""
        level = msg.metadata.get("reasoning_level")
        if level not in SUPPORTED_REASONING_LEVELS:
            return None

        self.reasoning_level = level
        from ragnarbot.config.loader import load_config, save_config
        config = load_config()
        config.agents.defaults.reasoning_level = level
        save_config(config)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=self._build_reasoning_message(),
            metadata={
                "raw_html": True,
                "edit_message_id": msg.metadata.get("callback_message_id"),
                "inline_keyboard": self._build_reasoning_keyboard(),
            },
        )

    def _handle_steering(self, msg: InboundMessage) -> OutboundMessage:
        """Show current steering mode with toggle button."""
        enabled = self.steering_enabled
        status = "Enabled" if enabled else "Disabled"
        toggle = "off" if enabled else "on"
        btn_text = "Disable" if enabled else "Enable"
        text = f"🧭 <b>Steering Mode</b>\n\nCurrent: {status}"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={
                "raw_html": True,
                "inline_keyboard": [[
                    {"text": btn_text, "callback_data": f"steering_mode:{toggle}"},
                ]],
            },
        )

    def _handle_set_steering_mode(self, msg: InboundMessage) -> OutboundMessage | None:
        """Toggle steering mode (from callback query)."""
        value = msg.metadata.get("steering_mode")
        if value not in ("on", "off"):
            return None

        self.steering_enabled = value == "on"
        from ragnarbot.config.loader import load_config, save_config
        config = load_config()
        config.agents.defaults.steering_enabled = self.steering_enabled
        save_config(config)

        status = "Enabled" if self.steering_enabled else "Disabled"
        text = f"✅ Steering mode: {status}"
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=text,
            metadata={
                "raw_html": True,
                "edit_message_id": msg.metadata.get("callback_message_id"),
            },
        )

    @staticmethod
    def _truncate(value: str, max_len: int = 200) -> str:
        """Truncate a string to max_len, appending '...' if needed."""
        if len(value) <= max_len:
            return value
        return value[:max_len] + "…"

    def _format_trace(self, tool_name: str, args: dict) -> str:
        """Format a tool call as a human-readable trace message (HTML).

        First arg = main (no label, just <code>).
        Secondary args = <i>label</i> + newline + <code>value</code>.
        Empty line between each arg block.
        """
        from html import escape

        # (emoji, label, list of (arg_key, max_chars) to display)
        # First entry = main arg (no label), rest = secondary (with label)
        tool_formats: dict[str, tuple[str, str, list[tuple[str, int]]]] = {
            "web_search": ("🌐", "Web search", [("query", 200)]),
            "web_fetch": ("🌐", "Web fetch", [("url", 200)]),
            "file_read": ("📄", "Read file", [("path", 200)]),
            "write_file": ("📝", "Write file", [("path", 200), ("content", 80)]),
            "edit_file": ("✏️", "Edit file", [("path", 200), ("old_text", 80), ("new_text", 80)]),
            "list_dir": ("📂", "List dir", [("path", 200)]),
            "exec": ("⚡", "Exec", [("command", 200)]),
            "exec_bg": ("⚡", "Exec (bg)", [("command", 200)]),
            "spawn": ("🤖", "Spawn", [("task", 120), ("label", 80)]),
            "send_photo": ("📸", "Send photo", [("file_path", 200), ("caption", 100)]),
            "send_video": ("🎬", "Send video", [("file_path", 200), ("caption", 100)]),
            "send_file": ("📎", "Send file", [("file_path", 200), ("caption", 100)]),
            "download_file": ("⬇️", "Download", [("file_id", 100)]),
            "config": ("⚙️", "Config", [("action", 120), ("path", 80), ("value", 80)]),
            "cron": ("⏰", "Cron", [("action", 120), ("message", 80)]),
        }

        fmt = tool_formats.get(tool_name)
        if fmt:
            emoji, label, arg_keys = fmt
            header = f"{emoji} <b>{label}</b>"
            blocks = []
            for idx, (key, max_len) in enumerate(arg_keys):
                if key not in args:
                    continue
                val = self._truncate(escape(str(args[key])), max_len)
                if idx == 0:
                    blocks.append(f"<code>{val}</code>")
                else:
                    blocks.append(f"<i>{escape(key)}</i>\n<code>{val}</code>")
            # Show any remaining args not listed in tool_formats
            shown_keys = {k for k, _ in arg_keys}
            for key, val in args.items():
                if key in shown_keys:
                    continue
                val_str = self._truncate(escape(str(val)))
                blocks.append(f"<i>{escape(key)}</i>\n<code>{val_str}</code>")
            if blocks:
                return header + "\n\n" + "\n\n".join(blocks)
            return header

        # Fallback: generic format — all args with italic labels
        header = f"🛠 <b>{escape(tool_name)}</b>"
        blocks = []
        for key, val in args.items():
            val_str = self._truncate(escape(str(val)))
            blocks.append(f"<i>{escape(key)}</i>\n<code>{val_str}</code>")
        if blocks:
            return header + "\n\n" + "\n\n".join(blocks)
        return header

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
        if self._fallback_state.fallback_mode and self._fallback_model:
            model_line = f"\U0001f504 Fallback mode \u26a1 <code>{self._fallback_model}</code>"
        else:
            model_line = f"\U0001f916 <code>{self.model}</code>"
        text = (
            f"\U0001f4ca <b>Context</b>\n\n"
            f"{model_line}\n"
            f"\U0001f4e6 {mode_labels[self.context_mode]} ({int(threshold * 100)}%)\n\n"
            f"\U0001f4c8 Usage: <b>{pct}%</b>  ({tokens_k} / {max_k})\n"
            f"\U0001f4be Compactions: {compactions}"
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
        memory_segment = None
        try:
            messages, _, memory_segment = await self.compactor.compact(
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

        if memory_segment is not None:
            self.memory_flush.enqueue_segment(session, memory_segment)

        await self._save_session_locked(session)

        if memory_segment is not None:
            await self.memory_flush.start_session_jobs(session.key)

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
        agent_tool = self.tools.get("agent")
        if isinstance(agent_tool, AgentTool):
            agent_tool.set_context(origin_channel, origin_chat_id)

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
        batch_used_fallback = False
        start_memory_jobs = False
        self._fb_error_notified = False  # reset per system message

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
                    messages, new_start, memory_segment = await self.compactor.compact(
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
                    if memory_segment is not None:
                        created_jobs = self.memory_flush.enqueue_segment(
                            session, memory_segment,
                        )
                        start_memory_jobs = start_memory_jobs or bool(created_jobs)
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
                response, used_fallback, primary_error = await self._chat_with_fallback(
                    session_key,
                    notify_channel=origin_channel,
                    notify_chat_id=origin_chat_id,
                    messages=api_messages,
                    tools=tools_defs,
                )

                # LLM call cancelled by stop
                if response is None:
                    logger.info(f"LLM call cancelled by stop for {session_key}")
                    stopped = True
                    break

                # Recovery notification
                if not used_fallback and self._fb_just_recovered:
                    self._fb_just_recovered = False
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=origin_channel, chat_id=origin_chat_id,
                        content=(
                            "\u2705 Primary model is back online. "
                            "Switched back to normal mode."
                        ),
                        metadata={"intermediate": True},
                    ))

                if used_fallback:
                    batch_used_fallback = True

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

                        if self.trace_mode:
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=origin_channel,
                                chat_id=origin_chat_id,
                                content=self._format_trace(
                                    tool_call.name, tool_call.arguments,
                                ),
                                metadata={"intermediate": True, "raw_html": True},
                            ))

                        try:
                            result = await self._execute_tool_with_tracking(
                                session_key, tool_call.name, tool_call.arguments,
                            )
                        except asyncio.CancelledError:
                            if not self._is_stopped(session_key):
                                raise
                            logger.info(
                                f"Tool execution cancelled by stop for {session_key}"
                            )
                            stopped = True
                            messages = self.context.add_tool_result(
                                messages, tool_call.id, tool_call.name, "[Stopped by user]",
                            )
                            for remaining in response.tool_calls[idx + 1:]:
                                messages = self.context.add_tool_result(
                                    messages, remaining.id, remaining.name, "[Stopped by user]",
                                )
                            break
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )

                    if stopped:
                        break
                    await self._inject_pending_steering(session_key, session, messages)
                else:
                    final_content = response.content
                    break
        finally:
            await self._save_session_locked(session)

        if stopped:
            await self._cleanup_stopped_run(session_key)

        # Fallback accounting — count once per system message
        outbound_content = final_content
        await self._record_fallback_batch(
            batch_used_fallback, origin_channel, origin_chat_id,
        )

        if batch_used_fallback and outbound_content and not self._fallback_state.fallback_mode:
            outbound_content += f"\n\n_\u26a1 fallback: {self._fallback_model}_"

        if not stopped:
            messages.append({"role": "assistant", "content": final_content or ""})

        state = self._get_run_state(session_key)
        steering_data = state.injected_steering if state is not None else []
        self._save_system_messages(session, messages, new_start, msg, steering_data)
        await self._save_session_locked(session)

        if start_memory_jobs:
            await self.memory_flush.start_session_jobs(session.key)

        if stopped or not outbound_content:
            return None

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=outbound_content
        )

    @staticmethod
    def _pending_update_system_content(payload: dict[str, Any]) -> str:
        """Build a system message for pending shared-binary updates."""
        old_ver = payload.get("old_version", "?")
        new_ver = payload.get("new_version", "?")
        summary = payload.get("summary", "Release notes available.")
        changelog_url = payload.get("changelog_url", "")
        excerpt = payload.get("body_excerpt", "")
        requires_restart = payload.get("requires_restart", True)

        if requires_restart:
            content = (
                f"[System: This profile is still running the old gateway process, "
                f"but the shared update is already installed. Tell the user that "
                f"the bot was updated from v{old_ver} to v{new_ver}. Mention this "
                f"summary: {summary}. Changelog: {changelog_url}. Ask whether they "
                f"want to restart now to apply the update in this profile. If they "
                f"agree, use the restart tool.]"
            )
        else:
            content = (
                f"[System: This profile was offline when the shared update was installed, "
                f"and it is already running the new version. Tell the user that the bot "
                f"was updated from v{old_ver} to v{new_ver}. Mention this summary: "
                f"{summary}. Changelog: {changelog_url}. Make it clear that no restart "
                f"is needed in this profile.]"
            )
        if excerpt:
            content += f"\n\n[Release excerpt]\n{excerpt}"
        return content

    async def queue_pending_update_notice(self) -> bool:
        """Queue a pending update notice using the last known active chat."""
        payload = load_pending_update()
        if not payload:
            return False

        channel, chat_id = pending_update_target(payload)
        if not channel or not chat_id:
            return False

        await self.bus.publish_inbound(InboundMessage(
            channel="system",
            sender_id="update",
            chat_id=f"{channel}:{chat_id}",
            content=self._pending_update_system_content(payload),
        ))
        return True

    async def deliver_pending_update_notice(
        self, channel: str | None = None, chat_id: str | None = None,
    ) -> bool:
        """Deliver a pending update notice immediately when a target chat is known."""
        payload = load_pending_update()
        if not payload:
            return False

        target_channel, target_chat_id = pending_update_target(payload)
        if target_channel and target_chat_id:
            if channel != target_channel or chat_id != target_chat_id:
                return False
        else:
            if not channel or not chat_id:
                return False
            payload = bind_pending_update_target(channel, chat_id) or payload
            target_channel, target_chat_id = pending_update_target(payload)
        if not target_channel or not target_chat_id:
            return False

        outbound = await self._process_system_message(InboundMessage(
            channel="system",
            sender_id="update",
            chat_id=f"{target_channel}:{target_chat_id}",
            content=self._pending_update_system_content(payload),
        ))
        if outbound:
            await self.bus.publish_outbound(outbound)
            if not payload.get("requires_restart", True):
                clear_pending_update()
            return True
        return False

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
        share no mutable state.  Agent tools are excluded
        (they don't make sense in non-interactive mode).
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
            safety_guard=self.exec_config.safety_guard,
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

        # Browser (shared manager — sessions persist across contexts)
        from ragnarbot.agent.tools.browser import BrowserTool
        reg.register(BrowserTool(manager=self.browser_manager))

        # Download file
        if self.media_manager:
            dl = DownloadFileTool(self.media_manager)
            dl.set_context(channel, f"{channel}:{chat_id}")
            reg.register(dl)

        # deliver_result — isolated-only
        deliver_tool = DeliverResultTool()
        reg.register(deliver_tool)

        return reg, deliver_tool

    def _build_cron_agent_tool_registry(
        self,
        definition: "AgentDefinition",
        channel: str,
        chat_id: str,
    ) -> tuple[ToolRegistry, DeliverResultTool]:
        """Build a tool registry for a cron job running with an agent profile.

        Starts from the full isolated tool registry, then filters to the
        agent's allowedTools (if not "all").  Auto-adds file_read when
        the agent has allowed_skills != "none".
        """
        if definition.allowed_tools == "all":
            # Agent allows everything — use full isolated registry
            return self._build_isolated_tool_registry(channel, chat_id)

        # Build full registry first, then keep only allowed tools + deliver_result
        full_reg, deliver_tool = self._build_isolated_tool_registry(channel, chat_id)

        allowed = set(definition.allowed_tools) if isinstance(
            definition.allowed_tools, list
        ) else {definition.allowed_tools}

        # Auto-add file_read when skills are allowed (needed to load SKILL.md)
        if definition.allowed_skills != "none":
            allowed.add("file_read")

        filtered_reg = ToolRegistry()
        for tool in full_reg._tools.values():
            if tool.name in allowed or tool.name == "deliver_result":
                filtered_reg.register(tool)

        # Ensure deliver_result is always present
        if not filtered_reg.has("deliver_result"):
            filtered_reg.register(deliver_tool)

        return filtered_reg, deliver_tool

    def _build_cron_agent_messages(
        self,
        definition: "AgentDefinition",
        message: str,
        session_metadata: dict,
    ) -> list[dict]:
        """Build messages for a cron job running with an agent profile.

        Combines the CRON_ISOLATED.md rules with the AGENT.md body into
        a single system prompt.
        """

        cron_ctx = session_metadata["cron_isolated"]

        # Load CRON_ISOLATED.md rules
        cron_rules = self.context._load_builtin_cron_isolated(cron_ctx)

        # Build combined system prompt: cron rules + agent instructions
        parts = [cron_rules, "---", f"# Agent Instructions\n\n{definition.body}"]

        # Inject skills summary if the agent has allowed_skills
        if definition.allowed_skills != "none":
            only = (
                None if definition.allowed_skills == "all"
                else list(definition.allowed_skills)
            )
            summary = self.context.skills.build_skills_summary(only=only)
            if summary:
                parts.append(
                    "---\n\n## Available Skills\n\n"
                    "The following skills are available. To load a skill's full "
                    "instructions, use `file_read` on its `<location>` path.\n\n"
                    + summary
                )

        system_prompt = "\n\n".join(parts)

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]

    async def process_cron_isolated(
        self,
        job_name: str,
        message: str,
        schedule_desc: str,
        channel: str,
        chat_id: str,
        agent_name: str | None = None,
    ) -> str | None:
        """Run an isolated cron job — fresh context, no session history.

        When *agent_name* is set the job runs with the named agent's
        instructions and (optionally restricted) tool set.  The system
        prompt combines CRON_ISOLATED.md rules with the AGENT.md body so
        the agent gets both cron execution rules AND its specialised
        instructions.

        Returns the result string (from deliver_result or final LLM text),
        or None if the agent produced no output.
        """
        # --- agent profile handling ---
        definition = None
        if agent_name:
            definition = self.context.agents.load_agent(agent_name)
            if not definition:
                logger.warning(
                    f"Cron job '{job_name}': agent '{agent_name}' not found, "
                    "falling back to default execution"
                )

        if definition:
            tools, deliver_tool = self._build_cron_agent_tool_registry(
                definition, channel, chat_id,
            )
        else:
            tools, deliver_tool = self._build_isolated_tool_registry(channel, chat_id)

        session_metadata = {
            "cron_isolated": {
                "job_name": job_name,
                "schedule_desc": schedule_desc,
                "task_message": message,
            },
        }

        if definition:
            # Build a combined prompt: CRON_ISOLATED.md rules + AGENT.md body
            messages = self._build_cron_agent_messages(
                definition, message, session_metadata,
            )
        else:
            messages = self.context.build_messages(
                history=[],
                current_message=message,
                channel=channel,
                chat_id=chat_id,
                session_metadata=session_metadata,
            )

        chat_kwargs: dict[str, Any] = {
            "messages": None,
            "tools": None,
        }
        if definition and definition.model != "default":
            chat_kwargs["model"] = definition.model
        if definition and definition.reasoning_level != "inherit":
            chat_kwargs["reasoning_level"] = definition.reasoning_level

        batch_used_fallback = False
        while True:
            tools_defs = tools.get_definitions()
            api_messages = [
                {k: v for k, v in m.items() if k != "_ts"} for m in messages
            ]
            chat_kwargs["messages"] = api_messages
            chat_kwargs["tools"] = tools_defs
            response, used_fallback, _ = await self._chat_with_fallback(
                None,
                **chat_kwargs,
            )
            if used_fallback:
                batch_used_fallback = True

            if response is None:
                await self._record_fallback_batch(batch_used_fallback, channel, chat_id)
                return None

            if response.has_tool_calls:
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
                    await self._record_fallback_batch(
                        batch_used_fallback, channel, chat_id,
                    )
                    return deliver_tool.result
            else:
                # Agent finished with text — use as fallback result
                await self._record_fallback_batch(
                    batch_used_fallback, channel, chat_id,
                )
                return response.content or None

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

        result = None
        batch_used_fallback = False
        while True:
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
            response, used_fallback, _ = await self._chat_with_fallback(
                None,
                messages=api_messages, tools=tools_defs,
            )
            if used_fallback:
                batch_used_fallback = True

            if response is None:
                break

            if response.has_tool_calls:
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

        await self._record_fallback_batch(batch_used_fallback, channel, chat_id)

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
