"""Abstract daemon manager interface and shared types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class DaemonStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    NOT_INSTALLED = "not_installed"


@dataclass
class DaemonInfo:
    status: DaemonStatus
    pid: int | None = None
    service_file: Path | None = None
    log_path: Path | None = None


class DaemonError(Exception):
    """Raised when a daemon operation fails."""


class DaemonManager(ABC):
    """Abstract base for platform-specific daemon managers."""

    @abstractmethod
    def install(self) -> None:
        """Install the daemon service file."""

    @abstractmethod
    def uninstall(self) -> None:
        """Remove the daemon service file."""

    @abstractmethod
    def start(self) -> None:
        """Start the daemon."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the daemon."""

    @abstractmethod
    def restart(self) -> None:
        """Stop and start the daemon."""

    @abstractmethod
    def status(self) -> DaemonInfo:
        """Get current daemon status."""

    @abstractmethod
    def is_installed(self) -> bool:
        """Check whether the service file exists."""
