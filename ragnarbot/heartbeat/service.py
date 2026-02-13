"""Heartbeat service - periodic agent wake-up to check for tasks."""

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from ragnarbot.agent.tools.heartbeat import parse_blocks

# Default interval: 30 minutes
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60


def _is_heartbeat_empty(content: str | None) -> bool:
    """Check if HEARTBEAT.md has no actionable task blocks."""
    if not content:
        return True
    return len(parse_blocks(content)) == 0


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Uses two-phase execution:
    1. Isolated phase: agent runs in its own context with a rolling session
    2. Delivery phase: if there's something to report, injects into user's chat
    """

    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[], Coroutine[Any, Any, tuple[str | None, str | None, str | None]]] | None = None,
        on_deliver: Callable[[str, str, str], Coroutine[Any, Any, None]] | None = None,
        on_complete: Callable[[str | None, str | None], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.on_heartbeat = on_heartbeat
        self.on_deliver = on_deliver
        self.on_complete = on_complete
        self.interval_s = interval_s
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        """Read HEARTBEAT.md content."""
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text()
            except Exception:
                return None
        return None

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Heartbeat started (every {self.interval_s}s)")

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        content = self._read_heartbeat_file()

        # Skip if HEARTBEAT.md has no task blocks
        if _is_heartbeat_empty(content):
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return

        logger.info("Heartbeat: checking for tasks...")

        if self.on_heartbeat:
            try:
                result, channel, chat_id = await self.on_heartbeat()

                if result and channel and chat_id:
                    logger.info("Heartbeat: delivering result")
                    if self.on_deliver:
                        await self.on_deliver(result, channel, chat_id)
                else:
                    logger.info("Heartbeat: OK (nothing to report)")

                if channel and chat_id and self.on_complete:
                    await self.on_complete(channel, chat_id)

            except Exception as e:
                logger.error(f"Heartbeat execution failed: {e}")

    async def trigger_now(self) -> tuple[str | None, str | None, str | None] | None:
        """Manually trigger a heartbeat. Returns (result, channel, chat_id)."""
        if self.on_heartbeat:
            return await self.on_heartbeat()
        return None
