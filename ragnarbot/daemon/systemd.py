"""Linux systemd --user daemon manager."""

import subprocess
from pathlib import Path

from ragnarbot.daemon.base import DaemonError, DaemonInfo, DaemonManager, DaemonStatus
from ragnarbot.daemon.resolve import get_log_dir, resolve_executable

UNIT_NAME = "ragnarbot-gateway.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_PATH = UNIT_DIR / UNIT_NAME


class SystemdManager(DaemonManager):

    @property
    def service_file(self) -> Path:
        return UNIT_PATH

    def install(self) -> None:
        exe = resolve_executable()
        exec_start = " ".join(exe) + " gateway"

        unit = f"""\
[Unit]
Description=ragnarbot gateway
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(unit)

        self._ctl("daemon-reload")
        self._ctl("enable", UNIT_NAME)

    def uninstall(self) -> None:
        if self.is_installed():
            try:
                self._ctl("disable", UNIT_NAME)
            except DaemonError:
                pass
            UNIT_PATH.unlink(missing_ok=True)
            self._ctl("daemon-reload")

    def start(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed. Run install() first.")
        self._ctl("start", UNIT_NAME)

    def stop(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed.")
        self._ctl("stop", UNIT_NAME)

    def restart(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed.")
        self._ctl("restart", UNIT_NAME)

    def status(self) -> DaemonInfo:
        if not self.is_installed():
            return DaemonInfo(status=DaemonStatus.NOT_INSTALLED)

        log_dir = get_log_dir()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", UNIT_NAME],
                capture_output=True, text=True,
            )
            active = result.stdout.strip() == "active"
        except FileNotFoundError:
            active = False

        pid = None
        if active:
            pid = self._get_pid()

        return DaemonInfo(
            status=DaemonStatus.RUNNING if active else DaemonStatus.STOPPED,
            pid=pid,
            service_file=UNIT_PATH,
            log_path=log_dir / "gateway.log",
        )

    def is_installed(self) -> bool:
        return UNIT_PATH.exists()

    def _get_pid(self) -> int | None:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", "-p", "MainPID", UNIT_NAME],
                capture_output=True, text=True,
            )
            # Output: MainPID=12345
            for line in result.stdout.splitlines():
                if line.startswith("MainPID="):
                    pid = int(line.split("=", 1)[1])
                    return pid if pid > 0 else None
        except (FileNotFoundError, ValueError):
            pass
        return None

    @staticmethod
    def _ctl(*args: str) -> None:
        try:
            subprocess.run(
                ["systemctl", "--user", *args],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise DaemonError(
                f"systemctl --user {' '.join(args)} failed: {e.stderr.strip()}"
            ) from e
