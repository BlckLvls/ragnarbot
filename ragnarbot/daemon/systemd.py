"""Linux systemd --user daemon manager."""

import subprocess
from pathlib import Path

from ragnarbot.daemon.base import DaemonError, DaemonInfo, DaemonManager, DaemonStatus
from ragnarbot.daemon.resolve import (
    get_log_dir,
    get_systemd_unit_dir,
    get_systemd_unit_name,
    get_systemd_unit_path,
    resolve_executable,
    service_cli_args,
)


class SystemdManager(DaemonManager):

    @property
    def service_file(self) -> Path:
        return get_systemd_unit_path()

    def install(self) -> None:
        exe = resolve_executable()
        unit_name = get_systemd_unit_name()
        unit_dir = get_systemd_unit_dir()
        unit_path = get_systemd_unit_path()
        exec_start = " ".join([*exe, *service_cli_args()])

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
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit)

        self._ctl("daemon-reload")
        self._ctl("enable", unit_name)

    def uninstall(self) -> None:
        unit_name = get_systemd_unit_name()
        unit_path = get_systemd_unit_path()
        if self.is_installed():
            try:
                self._ctl("disable", unit_name)
            except DaemonError:
                pass
            unit_path.unlink(missing_ok=True)
            self._ctl("daemon-reload")

    def start(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed. Run install() first.")
        self._ctl("start", get_systemd_unit_name())

    def stop(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed.")
        self._ctl("stop", get_systemd_unit_name())

    def restart(self) -> None:
        if not self.is_installed():
            raise DaemonError("Service not installed.")
        self._ctl("restart", get_systemd_unit_name())

    def status(self) -> DaemonInfo:
        if not self.is_installed():
            return DaemonInfo(status=DaemonStatus.NOT_INSTALLED)

        log_dir = get_log_dir()
        unit_name = get_systemd_unit_name()
        unit_path = get_systemd_unit_path()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", unit_name],
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
            service_file=unit_path,
            log_path=log_dir / "gateway.log",
        )

    def is_installed(self) -> bool:
        return get_systemd_unit_path().exists()

    def _get_pid(self) -> int | None:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", "-p", "MainPID", get_systemd_unit_name()],
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
