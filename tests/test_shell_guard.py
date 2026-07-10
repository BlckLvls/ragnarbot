"""Tests for ExecTool safety guard."""

import asyncio
import os
import shlex
import signal
import sys

import pytest

from ragnarbot.agent.tools.shell import ExecTool


def _descendant_command(pid_path, sleep_seconds: int = 30) -> str:
    code = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        f"time.sleep({sleep_seconds})"
    )
    child = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} "
        f"{shlex.quote(str(pid_path))}"
    )
    return f"{child} & wait"


async def _wait_for_file(path, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for {path}")
        await asyncio.sleep(0.02)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_pid_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _pid_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def _force_kill(pid: int | None) -> None:
    if pid is None or not _pid_exists(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TestShellGuardPatterns:
    """Test the _guard_command deny patterns on ExecTool."""

    def _guard(self, command: str) -> str | None:
        tool = ExecTool()
        return tool._guard_command(command, "/tmp")

    # -- format / mkfs / diskpart -------------------------------------------

    def test_blocks_format_command(self):
        assert self._guard("format C:") is not None

    def test_blocks_sudo_format(self):
        assert self._guard("sudo format C:") is not None

    def test_blocks_mkfs(self):
        assert self._guard("mkfs.ext4 /dev/sda1") is not None
        assert self._guard("sudo mkfs /dev/sdb") is not None

    def test_blocks_format_after_separator(self):
        assert self._guard("echo ok; format C:") is not None
        assert self._guard("echo ok && format C:") is not None
        assert self._guard("echo ok | format") is not None

    def test_allows_format_in_flags(self):
        """Issue #66: --output-format and similar flags must not be blocked."""
        assert self._guard("claude -p 'hello' --output-format json") is None
        assert self._guard("claude -p 'hello' --output-format text") is None
        assert self._guard("git log --format='%H %s'") is None
        assert self._guard("docker inspect --format '{{.State.Status}}'") is None
        assert self._guard("cargo build --message-format json") is None
        assert self._guard("pytest --log-format='%(message)s'") is None
        assert self._guard("echo '--output-format json'") is None

    # -- shutdown / reboot / poweroff ----------------------------------------

    def test_blocks_shutdown_command(self):
        assert self._guard("shutdown now") is not None
        assert self._guard("shutdown -h now") is not None

    def test_blocks_sudo_shutdown(self):
        assert self._guard("sudo shutdown -h now") is not None
        assert self._guard("sudo reboot") is not None

    def test_blocks_reboot_command(self):
        assert self._guard("reboot") is not None

    def test_blocks_poweroff_command(self):
        assert self._guard("poweroff") is not None

    def test_blocks_power_after_separator(self):
        assert self._guard("echo ok; shutdown now") is not None
        assert self._guard("echo ok && reboot") is not None
        assert self._guard("true | poweroff") is not None

    def test_allows_reboot_as_argument(self):
        """These are read-only diagnostic commands that should not be blocked."""
        assert self._guard("last reboot") is None
        assert self._guard("grep shutdown /var/log/syslog") is None
        assert self._guard("grep reboot /var/log/messages") is None
        assert self._guard("journalctl | grep reboot") is None
        assert self._guard("systemctl status reboot.target") is None
        assert self._guard("echo 'system shutdown required'") is None

    # -- rm patterns ---------------------------------------------------------

    def test_blocks_rm_rf(self):
        assert self._guard("rm -rf /") is not None
        assert self._guard("rm -r /tmp/dir") is not None
        assert self._guard("rm -f file.txt") is not None

    def test_allows_safe_rm(self):
        assert self._guard("rm file.txt") is None

    # -- dd pattern ----------------------------------------------------------

    def test_blocks_dd(self):
        assert self._guard("dd if=/dev/zero of=/dev/sda") is not None

    def test_allows_non_dd_commands(self):
        assert self._guard("echo hello") is None

    # -- fork bomb -----------------------------------------------------------

    def test_blocks_fork_bomb(self):
        assert self._guard(":(){ :|:& };:") is not None

    # -- general safe commands -----------------------------------------------

    def test_allows_common_safe_commands(self):
        assert self._guard("ls -la") is None
        assert self._guard("echo hello") is None
        assert self._guard("python script.py") is None
        assert self._guard("cat file.txt") is None
        assert self._guard("grep pattern file.txt") is None
        assert self._guard("pip install package") is None
        assert self._guard("git status") is None
        assert self._guard("uv run pytest") is None


class TestSafetyGuardToggle:
    """Test the safety_guard=False config option."""

    @pytest.mark.asyncio
    async def test_safety_guard_disabled_allows_dangerous(self):
        tool = ExecTool(safety_guard=False)
        result = await tool.execute("echo 'rm -rf /' test")
        assert "blocked" not in result.lower()

    @pytest.mark.asyncio
    async def test_safety_guard_enabled_blocks_dangerous(self):
        tool = ExecTool(safety_guard=True)
        result = await tool.execute("rm -rf /")
        assert "blocked" in result.lower()

    @pytest.mark.asyncio
    async def test_relative_working_dir_resolves_from_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        target = workspace / "docs"
        target.mkdir(parents=True)

        tool = ExecTool(working_dir=str(workspace))
        result = await tool.execute(
            f"{shlex.quote(sys.executable)} -c 'import os; print(os.getcwd())'",
            working_dir="docs",
        )

        assert str(target.resolve()) in result


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
class TestShellProcessLifecycle:
    @pytest.mark.asyncio
    async def test_timeout_kills_descendant_process(self, tmp_path):
        pid_path = tmp_path / "timeout-child.pid"
        pid = None
        try:
            result = await ExecTool(timeout=0.5, safety_guard=False).execute(
                _descendant_command(pid_path)
            )
            await _wait_for_file(pid_path)
            pid = int(pid_path.read_text())

            assert "timed out" in result.lower()
            assert await _wait_for_pid_exit(pid), "timed-out command left a child process alive"
        finally:
            _force_kill(pid)

    @pytest.mark.asyncio
    async def test_cancellation_kills_descendant_process(self, tmp_path):
        pid_path = tmp_path / "cancel-child.pid"
        pid = None
        task = asyncio.create_task(
            ExecTool(timeout=30, safety_guard=False).execute(_descendant_command(pid_path))
        )
        try:
            await _wait_for_file(pid_path)
            pid = int(pid_path.read_text())
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            assert await _wait_for_pid_exit(pid), "cancelled command left a child process alive"
        finally:
            if not task.done():
                task.cancel()
            _force_kill(pid)
