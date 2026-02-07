"""Platform detection and executable resolution."""

import shutil
import sys
from pathlib import Path

from ragnarbot.daemon.base import DaemonError


class UnsupportedPlatformError(DaemonError):
    """Raised on platforms without daemon support (e.g. Windows)."""


def detect_platform() -> str:
    """Return 'macos' or 'linux'. Raises on Windows."""
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        return "linux"
    raise UnsupportedPlatformError(
        f"Daemon management is not supported on {sys.platform}. "
        "Use 'ragnarbot gateway' to run in the foreground."
    )


def resolve_executable() -> list[str]:
    """Resolve the ragnarbot executable as a command list for service files.

    Tries in order:
    1. shutil.which('ragnarbot')
    2. <sys.executable parent>/ragnarbot
    3. sys.executable -m ragnarbot  (fallback)
    """
    # 1. On PATH
    which = shutil.which("ragnarbot")
    if which:
        return [which]

    # 2. Next to the Python interpreter
    sibling = Path(sys.executable).parent / "ragnarbot"
    if sibling.is_file():
        return [str(sibling)]

    # 3. Module invocation
    return [sys.executable, "-m", "ragnarbot"]


def get_log_dir() -> Path:
    """Return ~/.ragnarbot/logs/, creating it if needed."""
    log_dir = Path.home() / ".ragnarbot" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir
