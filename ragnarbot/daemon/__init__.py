"""Daemon management — factory and re-exports."""

from ragnarbot.daemon.base import DaemonError, DaemonInfo, DaemonManager, DaemonStatus
from ragnarbot.daemon.resolve import UnsupportedPlatformError, detect_platform

__all__ = [
    "DaemonError",
    "DaemonInfo",
    "DaemonManager",
    "DaemonStatus",
    "UnsupportedPlatformError",
    "get_manager",
]


def get_manager() -> DaemonManager:
    """Return the platform-appropriate DaemonManager."""
    platform = detect_platform()
    if platform == "macos":
        from ragnarbot.daemon.launchd import LaunchdManager
        return LaunchdManager()
    else:
        from ragnarbot.daemon.systemd import SystemdManager
        return SystemdManager()
