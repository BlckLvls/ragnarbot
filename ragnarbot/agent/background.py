"""Background process manager for async command execution."""

import asyncio
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ragnarbot.bus.events import InboundMessage

if TYPE_CHECKING:
    from ragnarbot.bus.queue import MessageBus
    from ragnarbot.config.schema import ExecToolConfig

# Hard-coded limits (no config needed)
MAX_CONCURRENT = 10
MAX_RUNTIME = 1200  # 20 minutes
OUTPUT_BUFFER_LINES = 1000
AUTO_DISMISS_SECONDS = 300  # 5 minutes


class JobState(str, Enum):
    running = "running"
    completed = "completed"
    error = "error"
    killed = "killed"
    consumed = "consumed"


@dataclass
class BgJob:
    job_id: str
    label: str
    command: str
    working_dir: str
    status: JobState
    started_at: float
    origin: dict[str, str]
    exit_code: int | None = None
    finished_at: float | None = None
    stdout_buffer: deque = field(default_factory=lambda: deque(maxlen=OUTPUT_BUFFER_LINES))
    stderr_buffer: deque = field(default_factory=lambda: deque(maxlen=OUTPUT_BUFFER_LINES))
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    # Poll-specific fields
    fire_at: float | None = None


# Deny patterns (same as ExecTool — duplicated to keep modules independent)
_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"\b(format|mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",
]


def _guard_command(
    command: str,
    cwd: str,
    restrict_to_workspace: bool = False,
    deny_patterns: list[str] | None = None,
    allow_patterns: list[str] | None = None,
) -> str | None:
    """Best-effort safety guard for destructive commands."""
    cmd = command.strip()
    lower = cmd.lower()

    patterns = deny_patterns if deny_patterns is not None else _DENY_PATTERNS
    for pattern in patterns:
        if re.search(pattern, lower):
            return "Error: Command blocked by safety guard (dangerous pattern detected)"

    if allow_patterns:
        if not any(re.search(p, lower) for p in allow_patterns):
            return "Error: Command blocked by safety guard (not in allowlist)"

    if restrict_to_workspace:
        if "..\\" in cmd or "../" in cmd:
            return "Error: Command blocked by safety guard (path traversal detected)"

        cwd_path = Path(cwd).resolve()
        win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
        posix_paths = re.findall(r"/[^\s\"']+", cmd)

        for raw in win_paths + posix_paths:
            try:
                p = Path(raw).resolve()
            except Exception:
                continue
            if cwd_path not in p.parents and p != cwd_path:
                return "Error: Command blocked by safety guard (path outside working dir)"

    return None


class BackgroundProcessManager:
    """Manages background shell processes with output capture and lifecycle."""

    def __init__(
        self,
        bus: "MessageBus",
        workspace: Path,
        exec_config: "ExecToolConfig | None" = None,
    ):
        from ragnarbot.config.schema import ExecToolConfig
        self.bus = bus
        self.workspace = workspace
        self.exec_config = exec_config or ExecToolConfig()
        self._jobs: dict[str, BgJob] = {}

    async def spawn(
        self,
        command: str,
        working_dir: str | None = None,
        label: str | None = None,
        origin: dict[str, str] | None = None,
    ) -> str:
        """Launch a command in the background. Returns job_id confirmation."""
        # Enforce concurrent limit
        running = sum(1 for j in self._jobs.values() if j.status == JobState.running)
        if running >= MAX_CONCURRENT:
            return f"Error: Too many concurrent background jobs ({MAX_CONCURRENT} limit)"

        cwd = working_dir or str(self.workspace)
        guard_error = _guard_command(
            command, cwd,
            restrict_to_workspace=self.exec_config.restrict_to_workspace,
        )
        if guard_error:
            return guard_error

        job_id = str(uuid.uuid4())[:8]
        display_label = label or (command[:30] + ("..." if len(command) > 30 else ""))
        origin = origin or {"channel": "cli", "chat_id": "direct"}

        job = BgJob(
            job_id=job_id,
            label=display_label,
            command=command,
            working_dir=cwd,
            status=JobState.running,
            started_at=time.time(),
            origin=origin,
        )
        self._jobs[job_id] = job

        bg_task = asyncio.create_task(self._run_job(job))
        job.task = bg_task
        bg_task.add_done_callback(lambda _: self._cleanup_stale())

        logger.info(f"Background job [{job_id}] started: {display_label}")
        return f"Background job started (id: {job_id}, label: {display_label})"

    async def _run_job(self, job: BgJob) -> None:
        """Execute subprocess, capture output, announce result on completion."""
        try:
            process = await asyncio.create_subprocess_shell(
                job.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=job.working_dir,
            )
            job.process = process

            async def _read_stream(stream: asyncio.StreamReader, buf: deque):
                async for line in stream:
                    buf.append(line.decode("utf-8", errors="replace").rstrip("\n"))

            readers = asyncio.gather(
                _read_stream(process.stdout, job.stdout_buffer),
                _read_stream(process.stderr, job.stderr_buffer),
            )

            try:
                await asyncio.wait_for(readers, timeout=MAX_RUNTIME)
                await process.wait()
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                job.status = JobState.killed
                job.exit_code = process.returncode
                job.finished_at = time.time()
                await self._announce_completion(job, timed_out=True)
                return

            job.exit_code = process.returncode
            job.finished_at = time.time()
            job.status = JobState.completed if process.returncode == 0 else JobState.error

        except Exception as e:
            logger.error(f"Background job [{job.job_id}] failed: {e}")
            job.status = JobState.error
            job.finished_at = time.time()
            job.stderr_buffer.append(f"Internal error: {e}")

        await self._announce_completion(job)

    async def _announce_completion(self, job: BgJob, timed_out: bool = False) -> None:
        """Publish a system message with job results for the agent to process."""
        runtime = (job.finished_at or time.time()) - job.started_at
        runtime_str = f"{runtime:.1f}s"

        last_lines = list(job.stdout_buffer)[-20:]
        stderr_lines = list(job.stderr_buffer)[-10:]
        output_text = "\n".join(last_lines) if last_lines else "(no stdout)"
        if stderr_lines:
            output_text += "\n\nSTDERR (last 10 lines):\n" + "\n".join(stderr_lines)

        timeout_note = " (TIMED OUT after 20min)" if timed_out else ""

        content = f"""[Background job '{job.label}' {job.status.value}{timeout_note}]

Command: {job.command}
Exit code: {job.exit_code}
Runtime: {runtime_str}

Output (last 20 lines):
{output_text}

Act on this result naturally. If the output contains file paths or URLs, share them with the user."""

        msg = InboundMessage(
            channel="system",
            sender_id="background",
            chat_id=f"{job.origin['channel']}:{job.origin['chat_id']}",
            content=content,
        )
        await self.bus.publish_inbound(msg)
        logger.debug(
            f"Background job [{job.job_id}] announced to "
            f"{job.origin['channel']}:{job.origin['chat_id']}"
        )

    async def schedule_poll(
        self,
        after: int,
        origin: dict[str, str] | None = None,
    ) -> str:
        """Schedule a status poll after N seconds."""
        origin = origin or {"channel": "cli", "chat_id": "direct"}
        job_id = str(uuid.uuid4())[:8]

        job = BgJob(
            job_id=job_id,
            label="poll",
            command="poll",
            working_dir=str(self.workspace),
            status=JobState.running,
            started_at=time.time(),
            origin=origin,
            fire_at=time.time() + after,
        )
        self._jobs[job_id] = job

        bg_task = asyncio.create_task(self._fire_poll(job))
        job.task = bg_task
        bg_task.add_done_callback(lambda _: self._cleanup_stale())

        logger.info(f"Poll [{job_id}] scheduled in {after}s")
        return f"Poll scheduled (id: {job_id}, fires in {after}s)"

    async def _fire_poll(self, poll_job: BgJob) -> None:
        """Sleep then publish status summary as system message."""
        delay = (poll_job.fire_at or 0) - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        poll_job.status = JobState.completed
        poll_job.finished_at = time.time()

        summary = self.get_status_summary()
        content = f"""[Background jobs status poll]

{summary}

Report this status to the user naturally."""

        msg = InboundMessage(
            channel="system",
            sender_id="background_poll",
            chat_id=f"{poll_job.origin['channel']}:{poll_job.origin['chat_id']}",
            content=content,
        )
        await self.bus.publish_inbound(msg)

    def get_output(self, job_id: str, lines: int = 20) -> str:
        """Return last N lines from a job's stdout buffer + status info."""
        job = self._jobs.get(job_id)
        if not job:
            return f"Error: No job with id '{job_id}'"
        if job.command == "poll":
            return f"Poll job {job_id}: status={job.status.value}"

        last_lines = list(job.stdout_buffer)[-lines:]
        stderr_lines = list(job.stderr_buffer)[-max(lines // 4, 5):]
        output = "\n".join(last_lines) if last_lines else "(no stdout yet)"
        if stderr_lines:
            output += "\n\nSTDERR:\n" + "\n".join(stderr_lines)

        runtime = (job.finished_at or time.time()) - job.started_at
        status_line = f"Status: {job.status.value} | Runtime: {runtime:.1f}s"
        if job.exit_code is not None:
            status_line += f" | Exit code: {job.exit_code}"

        return f"{status_line}\n\n{output}"

    def get_status_summary(self) -> str:
        """Formatted summary of all non-consumed jobs."""
        visible = [j for j in self._jobs.values() if j.status != JobState.consumed]
        if not visible:
            return "No background jobs."

        lines = []
        for j in visible:
            runtime = (j.finished_at or time.time()) - j.started_at
            line = f"[{j.job_id}] {j.label} — {j.status.value} ({runtime:.1f}s)"
            if j.exit_code is not None:
                line += f" exit={j.exit_code}"
            lines.append(line)
        return "\n".join(lines)

    async def kill(self, job_id: str) -> str:
        """Kill a running job or cancel a poll timer."""
        job = self._jobs.get(job_id)
        if not job:
            return f"Error: No job with id '{job_id}'"
        if job.status != JobState.running:
            return f"Job {job_id} is not running (status: {job.status.value})"

        # Poll timer — just cancel the asyncio task
        if job.command == "poll" and job.task:
            job.task.cancel()
            job.status = JobState.killed
            job.finished_at = time.time()
            return f"Poll {job_id} cancelled."

        # Real process
        if job.process:
            try:
                job.process.terminate()
                try:
                    await asyncio.wait_for(job.process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    job.process.kill()
                    await job.process.wait()
            except ProcessLookupError:
                pass

        job.status = JobState.killed
        job.exit_code = job.process.returncode if job.process else None
        job.finished_at = time.time()
        return f"Job {job_id} killed."

    def dismiss(self, job_id: str) -> str:
        """Mark a non-running job as consumed (hides from status)."""
        job = self._jobs.get(job_id)
        if not job:
            return f"Error: No job with id '{job_id}'"
        if job.status == JobState.running:
            return "Cannot dismiss a running job. Kill it first."
        job.status = JobState.consumed
        return f"Job {job_id} dismissed."

    def _cleanup_stale(self) -> None:
        """Auto-dismiss jobs completed more than AUTO_DISMISS_SECONDS ago."""
        now = time.time()
        for job in list(self._jobs.values()):
            if (
                job.status in (JobState.completed, JobState.error, JobState.killed)
                and job.finished_at
                and (now - job.finished_at) > AUTO_DISMISS_SECONDS
            ):
                job.status = JobState.consumed
