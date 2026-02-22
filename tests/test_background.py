"""Tests for background process manager and tools."""

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from ragnarbot.agent.background import (
    AUTO_DISMISS_SECONDS,
    MAX_CONCURRENT,
    OUTPUT_BUFFER_LINES,
    BackgroundProcessManager,
    BgJob,
    JobState,
    _guard_command,
)
from ragnarbot.agent.tools.background import (
    DismissTool,
    ExecBgTool,
    KillTool,
    OutputTool,
    PollTool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bus():
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    return bus


def _make_manager(bus=None):
    bus = bus or _make_bus()
    mgr = BackgroundProcessManager(bus=bus, workspace="/tmp")
    return mgr, bus


def _extract_job_id(result: str) -> str:
    """Extract job_id from spawn result string."""
    return result.split("Poll ")[1].split(" set")[0]


async def _kill_all(mgr: BackgroundProcessManager):
    """Kill all running jobs — use in test cleanup."""
    for job in list(mgr._jobs.values()):
        if job.status == JobState.running:
            if job.command == "poll" and job.task:
                job.task.cancel()
                job.status = JobState.killed
            elif job.process:
                try:
                    job.process.kill()
                except ProcessLookupError:
                    pass
            if job.task:
                job.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(job.task), timeout=1)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            job.status = JobState.killed


# ---------------------------------------------------------------------------
# Ring buffer tests
# ---------------------------------------------------------------------------

class TestRingBuffer:
    def test_capacity(self):
        buf = deque(maxlen=OUTPUT_BUFFER_LINES)
        for i in range(OUTPUT_BUFFER_LINES):
            buf.append(f"line {i}")
        assert len(buf) == OUTPUT_BUFFER_LINES

    def test_overflow(self):
        buf = deque(maxlen=5)
        for i in range(10):
            buf.append(f"line {i}")
        assert len(buf) == 5
        assert list(buf) == ["line 5", "line 6", "line 7", "line 8", "line 9"]

    def test_empty(self):
        buf = deque(maxlen=OUTPUT_BUFFER_LINES)
        assert len(buf) == 0
        assert list(buf) == []


# ---------------------------------------------------------------------------
# Command guard tests
# ---------------------------------------------------------------------------

class TestGuardCommand:
    def test_allows_safe_commands(self):
        assert _guard_command("ls -la", "/tmp") is None
        assert _guard_command("echo hello", "/tmp") is None
        assert _guard_command("python script.py", "/tmp") is None

    def test_blocks_dangerous_commands(self):
        assert "blocked" in _guard_command("rm -rf /", "/tmp")
        assert "blocked" in _guard_command("dd if=/dev/zero of=disk", "/tmp")
        assert "blocked" in _guard_command("shutdown now", "/tmp")

    def test_allows_format_in_flags(self):
        """Regression: --output-format and similar flags must not be blocked."""
        assert _guard_command("claude -p 'hello' --output-format json", "/tmp") is None
        assert _guard_command("git log --format='%H %s'", "/tmp") is None
        assert _guard_command("docker inspect --format '{{.State}}'", "/tmp") is None
        assert _guard_command("cargo build --message-format json", "/tmp") is None

    def test_blocks_format_as_command(self):
        assert _guard_command("format C:", "/tmp") is not None
        assert _guard_command("sudo mkfs.ext4 /dev/sda1", "/tmp") is not None
        assert _guard_command("echo ok; format C:", "/tmp") is not None

    def test_allows_reboot_as_argument(self):
        """Regression: 'last reboot' and grep for reboot/shutdown must not be blocked."""
        assert _guard_command("last reboot", "/tmp") is None
        assert _guard_command("grep shutdown /var/log/syslog", "/tmp") is None
        assert _guard_command("journalctl | grep reboot", "/tmp") is None

    def test_blocks_reboot_as_command(self):
        assert _guard_command("reboot", "/tmp") is not None
        assert _guard_command("sudo shutdown -h now", "/tmp") is not None
        assert _guard_command("echo ok; reboot", "/tmp") is not None
        assert _guard_command("echo ok && poweroff", "/tmp") is not None

    def test_workspace_restriction(self):
        result = _guard_command("cat ../../../etc/passwd", "/tmp", restrict_to_workspace=True)
        assert result is not None
        assert _guard_command("cat file.txt", "/tmp", restrict_to_workspace=True) is None


# ---------------------------------------------------------------------------
# Manager tests
# ---------------------------------------------------------------------------

class TestBackgroundProcessManager:
    @pytest.mark.asyncio
    async def test_spawn_returns_job_id(self):
        mgr, bus = _make_manager()
        result = await mgr.spawn("echo hello", label="test echo")
        assert "Background job started" in result
        assert "id:" in result
        await asyncio.sleep(0.3)  # let it finish

    @pytest.mark.asyncio
    async def test_concurrent_limit(self):
        """Test that exceeding MAX_CONCURRENT is rejected."""
        mgr, bus = _make_manager()
        # Fake running jobs by directly inserting BgJob entries
        for i in range(MAX_CONCURRENT):
            job = BgJob(
                job_id=f"fake-{i}",
                label=f"fake-{i}",
                command="sleep 999",
                working_dir="/tmp",
                status=JobState.running,
                started_at=time.time(),
                origin={"channel": "test", "chat_id": "1"},
            )
            mgr._jobs[job.job_id] = job
        # Next one should be rejected
        result = await mgr.spawn("echo overflow")
        assert "Too many" in result

    @pytest.mark.asyncio
    async def test_command_guard_blocks(self):
        mgr, bus = _make_manager()
        result = await mgr.spawn("rm -rf /important")
        assert "blocked" in result

    @pytest.mark.asyncio
    async def test_kill_running_job(self):
        mgr, bus = _make_manager()
        result = await mgr.spawn("sleep 60", label="sleeper")
        job_id = _extract_job_id(result)
        await asyncio.sleep(0.1)
        kill_result = await mgr.kill(job_id)
        assert "killed" in kill_result.lower()
        await asyncio.sleep(0.2)  # let cleanup settle

    @pytest.mark.asyncio
    async def test_output_on_running_job(self):
        mgr, bus = _make_manager()
        result = await mgr.spawn("echo hello && sleep 2", label="echo-sleep")
        job_id = _extract_job_id(result)
        await asyncio.sleep(0.3)
        output = mgr.get_output(job_id)
        assert "Status:" in output
        await mgr.kill(job_id)
        await asyncio.sleep(0.2)

    @pytest.mark.asyncio
    async def test_output_unknown_job(self):
        mgr, _ = _make_manager()
        output = mgr.get_output("nonexistent")
        assert "Error" in output

    @pytest.mark.asyncio
    async def test_dismiss_completed_job(self):
        mgr, bus = _make_manager()
        result = await mgr.spawn("echo done", label="quick")
        job_id = _extract_job_id(result)
        await asyncio.sleep(0.5)
        dismiss_result = mgr.dismiss(job_id)
        assert "dismissed" in dismiss_result.lower()

    @pytest.mark.asyncio
    async def test_dismiss_running_job_fails(self):
        mgr, bus = _make_manager()
        result = await mgr.spawn("sleep 60", label="long")
        job_id = _extract_job_id(result)
        await asyncio.sleep(0.1)
        dismiss_result = mgr.dismiss(job_id)
        assert "Cannot dismiss" in dismiss_result
        await mgr.kill(job_id)
        await asyncio.sleep(0.2)

    @pytest.mark.asyncio
    async def test_poll_scheduling(self):
        mgr, bus = _make_manager()
        result = await mgr.schedule_poll(after=1, origin={"channel": "test", "chat_id": "1"})
        assert "Poll" in result
        await asyncio.sleep(1.5)
        bus.publish_inbound.assert_called()

    @pytest.mark.asyncio
    async def test_status_summary_empty(self):
        mgr, _ = _make_manager()
        assert mgr.get_status_summary() == "No background jobs."

    @pytest.mark.asyncio
    async def test_status_summary_shows_jobs(self):
        """Test that running jobs appear in summary."""
        mgr, _ = _make_manager()
        # Use a fake job to avoid real subprocess
        job = BgJob(
            job_id="sum-1",
            label="running-job",
            command="sleep 999",
            working_dir="/tmp",
            status=JobState.running,
            started_at=time.time(),
            origin={"channel": "test", "chat_id": "1"},
        )
        mgr._jobs[job.job_id] = job
        summary = mgr.get_status_summary()
        assert "running-job" in summary
        assert "running" in summary

    @pytest.mark.asyncio
    async def test_completion_announces(self):
        mgr, bus = _make_manager()
        await mgr.spawn("echo hello", label="announcer")
        await asyncio.sleep(0.5)
        bus.publish_inbound.assert_called()
        call_args = bus.publish_inbound.call_args[0][0]
        assert call_args.channel == "system"
        assert call_args.sender_id == "background"

    @pytest.mark.asyncio
    async def test_kill_nonexistent(self):
        mgr, _ = _make_manager()
        result = await mgr.kill("nope")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_kill_not_running(self):
        mgr, bus = _make_manager()
        await mgr.spawn("echo done", label="quick")
        await asyncio.sleep(0.5)
        job_id = list(mgr._jobs.keys())[0]
        result = await mgr.kill(job_id)
        assert "not running" in result

    @pytest.mark.asyncio
    async def test_poll_rejects_duplicate(self):
        """Scheduling a second poll for the same origin is rejected."""
        mgr, bus = _make_manager()
        origin = {"channel": "test", "chat_id": "1"}
        first = await mgr.schedule_poll(after=60, origin=origin)
        assert "Poll" in first
        second = await mgr.schedule_poll(after=60, origin=origin)
        assert "Poll already active" in second
        # Cleanup
        job_id = first.split("Poll ")[1].split(" set")[0]
        await mgr.kill(job_id)

    @pytest.mark.asyncio
    async def test_poll_allows_after_completion(self):
        """A new poll can be scheduled once the previous one completes."""
        mgr, bus = _make_manager()
        origin = {"channel": "test", "chat_id": "1"}
        first = await mgr.schedule_poll(after=0, origin=origin)
        assert "Poll" in first
        await asyncio.sleep(0.3)  # let the poll fire and complete
        second = await mgr.schedule_poll(after=60, origin=origin)
        assert "Poll" in second
        # Cleanup
        job_id = second.split("Poll ")[1].split(" set")[0]
        await mgr.kill(job_id)

    @pytest.mark.asyncio
    async def test_cleanup_stale(self):
        mgr, _ = _make_manager()
        job = BgJob(
            job_id="old",
            label="old job",
            command="echo",
            working_dir="/tmp",
            status=JobState.completed,
            started_at=time.time() - AUTO_DISMISS_SECONDS - 100,
            finished_at=time.time() - AUTO_DISMISS_SECONDS - 50,
            origin={"channel": "test", "chat_id": "1"},
        )
        mgr._jobs["old"] = job
        mgr._cleanup_stale()
        assert job.status == JobState.consumed


# ---------------------------------------------------------------------------
# Tool parameter validation tests
# ---------------------------------------------------------------------------

class TestToolParameters:
    def test_exec_bg_required_params(self):
        mgr, _ = _make_manager()
        tool = ExecBgTool(manager=mgr)
        errors = tool.validate_params({})
        assert any("command" in e for e in errors)
        errors = tool.validate_params({"command": "echo hi"})
        assert errors == []

    def test_poll_required_params(self):
        mgr, _ = _make_manager()
        tool = PollTool(manager=mgr)
        errors = tool.validate_params({})
        assert any("after" in e for e in errors)
        errors = tool.validate_params({"after": 10})
        assert errors == []

    def test_output_required_params(self):
        mgr, _ = _make_manager()
        tool = OutputTool(manager=mgr)
        errors = tool.validate_params({})
        assert any("job_id" in e for e in errors)
        errors = tool.validate_params({"job_id": "abc123"})
        assert errors == []

    def test_kill_required_params(self):
        mgr, _ = _make_manager()
        tool = KillTool(manager=mgr)
        errors = tool.validate_params({})
        assert any("job_id" in e for e in errors)

    def test_dismiss_required_params(self):
        mgr, _ = _make_manager()
        tool = DismissTool(manager=mgr)
        errors = tool.validate_params({})
        assert any("job_id" in e for e in errors)


# ---------------------------------------------------------------------------
# Tool delegation tests
# ---------------------------------------------------------------------------

class TestToolDelegation:
    @pytest.mark.asyncio
    async def test_exec_bg_delegates(self):
        mgr, _ = _make_manager()
        tool = ExecBgTool(manager=mgr)
        tool.set_context("telegram", "12345")
        result = await tool.execute(command="echo hi", label="test")
        assert "Background job started" in result
        await asyncio.sleep(0.3)

    @pytest.mark.asyncio
    async def test_poll_delegates(self):
        mgr, bus = _make_manager()
        tool = PollTool(manager=mgr)
        tool.set_context("telegram", "12345")
        result = await tool.execute(after=60)
        assert "Poll" in result
        # Cancel the poll so it doesn't linger
        job_id = result.split("Poll ")[1].split(" set")[0]
        await mgr.kill(job_id)

    @pytest.mark.asyncio
    async def test_output_delegates(self):
        mgr, _ = _make_manager()
        tool = OutputTool(manager=mgr)
        result = await tool.execute(job_id="nonexistent")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_kill_delegates(self):
        mgr, _ = _make_manager()
        tool = KillTool(manager=mgr)
        result = await tool.execute(job_id="nonexistent")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_dismiss_delegates(self):
        mgr, _ = _make_manager()
        tool = DismissTool(manager=mgr)
        result = await tool.execute(job_id="nonexistent")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_exec_bg_context_propagates(self):
        mgr, bus = _make_manager()
        tool = ExecBgTool(manager=mgr)
        tool.set_context("telegram", "99999")
        await tool.execute(command="echo context_test")
        await asyncio.sleep(0.5)
        bus.publish_inbound.assert_called()
        call_args = bus.publish_inbound.call_args[0][0]
        assert "telegram:99999" in call_args.chat_id
