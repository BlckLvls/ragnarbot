"""macOS launchd daemon manager."""

import plistlib
import subprocess
from pathlib import Path

from ragnarbot.daemon.base import DaemonError, DaemonInfo, DaemonManager, DaemonStatus
from ragnarbot.daemon.resolve import get_log_dir, resolve_executable

LABEL = "com.ragnarbot.gateway"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


class LaunchdManager(DaemonManager):

    @property
    def service_file(self) -> Path:
        return PLIST_PATH

    def install(self) -> None:
        exe = resolve_executable()
        log_dir = get_log_dir()

        plist = {
            "Label": LABEL,
            "ProgramArguments": [*exe, "gateway"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log_dir / "gateway.log"),
            "StandardErrorPath": str(log_dir / "gateway.err.log"),
        }

        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PLIST_PATH, "wb") as f:
            plistlib.dump(plist, f)

    def uninstall(self) -> None:
        if PLIST_PATH.exists():
            PLIST_PATH.unlink()

    def start(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed. Run install() first.")
        try:
            subprocess.run(
                ["launchctl", "load", str(PLIST_PATH)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise DaemonError(f"Failed to start daemon: {e.stderr.strip()}") from e

    def stop(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed.")
        try:
            subprocess.run(
                ["launchctl", "unload", str(PLIST_PATH)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise DaemonError(f"Failed to stop daemon: {e.stderr.strip()}") from e

    def restart(self) -> None:
        info = self.status()
        if info.status == DaemonStatus.RUNNING:
            self.stop()
        self.start()

    def status(self) -> DaemonInfo:
        if not self.is_installed():
            return DaemonInfo(status=DaemonStatus.NOT_INSTALLED)

        log_dir = get_log_dir()
        try:
            result = subprocess.run(
                ["launchctl", "list", LABEL],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                pid = self._parse_pid(result.stdout)
                return DaemonInfo(
                    status=DaemonStatus.RUNNING,
                    pid=pid,
                    service_file=PLIST_PATH,
                    log_path=log_dir / "gateway.log",
                )
        except FileNotFoundError:
            pass

        return DaemonInfo(
            status=DaemonStatus.STOPPED,
            service_file=PLIST_PATH,
            log_path=log_dir / "gateway.log",
        )

    def is_installed(self) -> bool:
        return PLIST_PATH.exists()

    @staticmethod
    def _parse_pid(output: str) -> int | None:
        """Extract PID from launchctl list output."""
        for line in output.splitlines():
            if '"PID"' in line or "PID" in line:
                parts = line.strip().rstrip(";").split("=")
                if len(parts) == 2:
                    try:
                        return int(parts[1].strip().rstrip(";"))
                    except ValueError:
                        pass
        return None
