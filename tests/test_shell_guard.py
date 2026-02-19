"""Tests for ExecTool safety guard."""

import pytest

from ragnarbot.agent.tools.shell import ExecTool


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
