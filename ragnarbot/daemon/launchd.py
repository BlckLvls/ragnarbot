"""macOS launchd daemon manager."""

import plistlib
import subprocess
from pathlib import Path

from ragnarbot.daemon.base import DaemonError, DaemonInfo, DaemonManager, DaemonStatus
from ragnarbot.daemon.resolve import (
    get_launchd_label,
    get_launchd_plist_path,
    get_log_dir,
    resolve_executable,
    service_cli_args,
)


class LaunchdManager(DaemonManager):

    @property
    def service_file(self) -> Path:
        return get_launchd_plist_path()

    def install(self) -> None:
        exe = resolve_executable()
        log_dir = get_log_dir()
        plist_path = get_launchd_plist_path()
        label = get_launchd_label()

        plist = {
            "Label": label,
            "ProgramArguments": [*exe, *service_cli_args()],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log_dir / "gateway.log"),
            "StandardErrorPath": str(log_dir / "gateway.err.log"),
        }

        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)

    def uninstall(self) -> None:
        plist_path = get_launchd_plist_path()
        if plist_path.exists():
            plist_path.unlink()

    def start(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed. Run install() first.")
        plist_path = get_launchd_plist_path()
        try:
            subprocess.run(
                ["launchctl", "load", str(plist_path)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise DaemonError(f"Failed to start daemon: {e.stderr.strip()}") from e

    def stop(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed.")
        plist_path = get_launchd_plist_path()
        try:
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
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
        plist_path = get_launchd_plist_path()
        label = get_launchd_label()
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                pid = self._parse_pid(result.stdout)
                return DaemonInfo(
                    status=DaemonStatus.RUNNING,
                    pid=pid,
                    service_file=plist_path,
                    log_path=log_dir / "gateway.log",
                )
        except FileNotFoundError:
            pass

        return DaemonInfo(
            status=DaemonStatus.STOPPED,
            service_file=plist_path,
            log_path=log_dir / "gateway.log",
        )

    def is_installed(self) -> bool:
        return get_launchd_plist_path().exists()

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
