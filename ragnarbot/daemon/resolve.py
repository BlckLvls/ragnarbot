"""Platform detection, executable resolution, and PATH enrichment."""

import os
import shutil
import subprocess
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


# ---------------------------------------------------------------------------
# PATH enrichment for daemon environments
# ---------------------------------------------------------------------------

_WELL_KNOWN_DIRS = [
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "~/.local/bin",
    "~/.cargo/bin",
    "~/.nvm/current/bin",
    "~/.deno/bin",
    "~/go/bin",
    "~/.bun/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/snap/bin",
]


def _probe_login_shell() -> list[str]:
    """Run the user's login shell to capture the full PATH."""
    shell = os.environ.get("SHELL", "/bin/sh")
    try:
        result = subprocess.run(
            [shell, "-l", "-c", "echo $PATH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [p for p in result.stdout.strip().split(":") if p]
    except Exception:
        pass
    return []


def _probe_path_helper() -> list[str]:
    """Run macOS path_helper to get system-configured paths."""
    if sys.platform != "darwin":
        return []
    helper = "/usr/libexec/path_helper"
    if not os.path.isfile(helper):
        return []
    try:
        result = subprocess.run(
            [helper, "-s"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            # Output format: PATH="..."; export PATH;
            line = result.stdout.strip()
            if line.startswith('PATH="') and '"' in line[6:]:
                raw = line[6:line.index('"', 6)]
                return [p for p in raw.split(":") if p]
    except Exception:
        pass
    return []


def _well_known_dirs() -> list[str]:
    """Return well-known tool directories that exist on disk."""
    dirs: list[str] = []
    for d in _WELL_KNOWN_DIRS:
        expanded = os.path.expanduser(d)
        if os.path.isdir(expanded):
            dirs.append(expanded)
    return dirs


def resolve_path() -> None:
    """Enrich os.environ["PATH"] with the user's full PATH.

    Runs a 3-step fallback chain. Each step is independent and
    catches its own errors. The function never raises.
    """
    try:
        current = os.environ.get("PATH", "")
        current_set = set(current.split(":")) if current else set()

        discovered: list[str] = []

        # 1. Login shell probe
        for p in _probe_login_shell():
            if p not in current_set and p not in discovered:
                discovered.append(p)

        # 2. macOS path_helper
        for p in _probe_path_helper():
            if p not in current_set and p not in discovered:
                discovered.append(p)

        # 3. Well-known directories
        for p in _well_known_dirs():
            if p not in current_set and p not in discovered:
                discovered.append(p)

        if discovered:
            os.environ["PATH"] = ":".join(discovered) + ":" + current
    except Exception:
        pass
