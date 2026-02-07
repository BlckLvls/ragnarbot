"""Tests for the daemon management module."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ragnarbot.daemon.base import DaemonError, DaemonInfo, DaemonStatus
from ragnarbot.daemon.resolve import (
    UnsupportedPlatformError,
    detect_platform,
    get_log_dir,
    resolve_executable,
)


class TestDetectPlatform:
    def test_macos(self):
        with patch.object(sys, "platform", "darwin"):
            assert detect_platform() == "macos"

    def test_linux(self):
        with patch.object(sys, "platform", "linux"):
            assert detect_platform() == "linux"

    def test_windows_raises(self):
        with patch.object(sys, "platform", "win32"):
            with pytest.raises(UnsupportedPlatformError):
                detect_platform()


class TestResolveExecutable:
    def test_which_found(self):
        with patch("ragnarbot.daemon.resolve.shutil.which", return_value="/usr/local/bin/ragnarbot"):
            assert resolve_executable() == ["/usr/local/bin/ragnarbot"]

    def test_sibling_found(self, tmp_path):
        fake_python = tmp_path / "python3"
        fake_python.touch()
        sibling = tmp_path / "ragnarbot"
        sibling.touch()

        with (
            patch("ragnarbot.daemon.resolve.shutil.which", return_value=None),
            patch("ragnarbot.daemon.resolve.sys.executable", str(fake_python)),
        ):
            assert resolve_executable() == [str(sibling)]

    def test_module_fallback(self, tmp_path):
        fake_python = tmp_path / "python3"
        fake_python.touch()

        with (
            patch("ragnarbot.daemon.resolve.shutil.which", return_value=None),
            patch("ragnarbot.daemon.resolve.sys.executable", str(fake_python)),
        ):
            result = resolve_executable()
            assert result == [str(fake_python), "-m", "ragnarbot"]


class TestGetLogDir:
    def test_creates_dir(self, tmp_path):
        log_dir = tmp_path / ".ragnarbot" / "logs"
        with patch("ragnarbot.daemon.resolve.Path.home", return_value=tmp_path):
            result = get_log_dir()
            assert result == log_dir
            assert log_dir.is_dir()


class TestDaemonInfo:
    def test_defaults(self):
        info = DaemonInfo(status=DaemonStatus.NOT_INSTALLED)
        assert info.pid is None
        assert info.service_file is None
        assert info.log_path is None

    def test_running(self):
        info = DaemonInfo(
            status=DaemonStatus.RUNNING,
            pid=1234,
            service_file=Path("/tmp/test.plist"),
        )
        assert info.status == DaemonStatus.RUNNING
        assert info.pid == 1234


class TestGetManager:
    def test_macos_returns_launchd(self):
        with patch("ragnarbot.daemon.detect_platform", return_value="macos"):
            from ragnarbot.daemon import get_manager
            from ragnarbot.daemon.launchd import LaunchdManager
            manager = get_manager()
            assert isinstance(manager, LaunchdManager)

    def test_linux_returns_systemd(self):
        with patch("ragnarbot.daemon.detect_platform", return_value="linux"):
            from ragnarbot.daemon import get_manager
            from ragnarbot.daemon.systemd import SystemdManager
            manager = get_manager()
            assert isinstance(manager, SystemdManager)


class TestLaunchdManager:
    def test_install_creates_plist(self, tmp_path):
        import plistlib

        from ragnarbot.daemon.launchd import LaunchdManager

        plist_path = tmp_path / "com.ragnarbot.gateway.plist"
        log_dir = tmp_path / "logs"

        with (
            patch("ragnarbot.daemon.launchd.PLIST_PATH", plist_path),
            patch("ragnarbot.daemon.launchd.resolve_executable", return_value=["/usr/bin/ragnarbot"]),
            patch("ragnarbot.daemon.launchd.get_log_dir", return_value=log_dir),
        ):
            manager = LaunchdManager()
            manager.install()

        assert plist_path.exists()
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        assert plist["Label"] == "com.ragnarbot.gateway"
        assert plist["ProgramArguments"] == ["/usr/bin/ragnarbot", "gateway"]
        assert plist["RunAtLoad"] is True
        assert plist["KeepAlive"] is True

    def test_uninstall_removes_plist(self, tmp_path):
        from ragnarbot.daemon.launchd import LaunchdManager

        plist_path = tmp_path / "com.ragnarbot.gateway.plist"
        plist_path.touch()

        with patch("ragnarbot.daemon.launchd.PLIST_PATH", plist_path):
            manager = LaunchdManager()
            manager.uninstall()

        assert not plist_path.exists()

    def test_is_installed(self, tmp_path):
        from ragnarbot.daemon.launchd import LaunchdManager

        plist_path = tmp_path / "com.ragnarbot.gateway.plist"

        with patch("ragnarbot.daemon.launchd.PLIST_PATH", plist_path):
            manager = LaunchdManager()
            assert not manager.is_installed()
            plist_path.touch()
            assert manager.is_installed()

    def test_status_not_installed(self, tmp_path):
        from ragnarbot.daemon.launchd import LaunchdManager

        plist_path = tmp_path / "com.ragnarbot.gateway.plist"
        with patch("ragnarbot.daemon.launchd.PLIST_PATH", plist_path):
            manager = LaunchdManager()
            info = manager.status()
            assert info.status == DaemonStatus.NOT_INSTALLED

    def test_start_raises_if_not_installed(self, tmp_path):
        from ragnarbot.daemon.launchd import LaunchdManager

        plist_path = tmp_path / "com.ragnarbot.gateway.plist"
        with patch("ragnarbot.daemon.launchd.PLIST_PATH", plist_path):
            manager = LaunchdManager()
            with pytest.raises(DaemonError, match="not installed"):
                manager.start()


class TestSystemdManager:
    def test_install_creates_unit(self, tmp_path):
        from ragnarbot.daemon.systemd import SystemdManager

        unit_path = tmp_path / "ragnarbot-gateway.service"

        with (
            patch("ragnarbot.daemon.systemd.UNIT_DIR", tmp_path),
            patch("ragnarbot.daemon.systemd.UNIT_PATH", unit_path),
            patch("ragnarbot.daemon.systemd.resolve_executable", return_value=["/usr/bin/ragnarbot"]),
            patch("ragnarbot.daemon.systemd.SystemdManager._ctl") as mock_ctl,
        ):
            manager = SystemdManager()
            manager.install()

        assert unit_path.exists()
        content = unit_path.read_text()
        assert "ExecStart=/usr/bin/ragnarbot gateway" in content
        assert "Restart=on-failure" in content
        assert mock_ctl.call_count == 2  # daemon-reload + enable

    def test_is_installed(self, tmp_path):
        from ragnarbot.daemon.systemd import SystemdManager

        unit_path = tmp_path / "ragnarbot-gateway.service"

        with patch("ragnarbot.daemon.systemd.UNIT_PATH", unit_path):
            manager = SystemdManager()
            assert not manager.is_installed()
            unit_path.touch()
            assert manager.is_installed()

    def test_status_not_installed(self, tmp_path):
        from ragnarbot.daemon.systemd import SystemdManager

        unit_path = tmp_path / "ragnarbot-gateway.service"
        with patch("ragnarbot.daemon.systemd.UNIT_PATH", unit_path):
            manager = SystemdManager()
            info = manager.status()
            assert info.status == DaemonStatus.NOT_INSTALLED

    def test_start_raises_if_not_installed(self, tmp_path):
        from ragnarbot.daemon.systemd import SystemdManager

        unit_path = tmp_path / "ragnarbot-gateway.service"
        with patch("ragnarbot.daemon.systemd.UNIT_PATH", unit_path):
            manager = SystemdManager()
            with pytest.raises(DaemonError, match="not installed"):
                manager.start()


class TestDaemonConfig:
    def test_default_disabled(self):
        from ragnarbot.config.schema import Config
        config = Config()
        assert config.daemon.enabled is False

    def test_set_enabled(self):
        from ragnarbot.config.schema import Config
        config = Config()
        config.daemon.enabled = True
        assert config.daemon.enabled is True
