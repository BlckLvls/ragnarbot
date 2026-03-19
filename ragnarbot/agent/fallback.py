"""Fallback model state tracking."""

import json
import time
from dataclasses import dataclass, field

from ragnarbot.instance import ensure_instance_root


def _state_file():
    return ensure_instance_root().fallback_state_path


@dataclass
class FallbackState:
    """Tracks primary/fallback provider state for automatic failover."""

    consecutive_failures: int = 0
    fallback_mode: bool = False
    last_primary_probe: float = field(default_factory=time.monotonic)

    def save(self) -> None:
        """Persist to disk. Only call when state changes."""
        state_file = _state_file()
        if self.consecutive_failures == 0 and not self.fallback_mode:
            state_file.unlink(missing_ok=True)
            return
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "consecutive_failures": self.consecutive_failures,
            "fallback_mode": self.fallback_mode,
        }))

    @classmethod
    def load(cls) -> "FallbackState":
        """Load from disk, or return fresh state if no file."""
        state_file = _state_file()
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                return cls(
                    consecutive_failures=data.get("consecutive_failures", 0),
                    fallback_mode=data.get("fallback_mode", False),
                )
            except (json.JSONDecodeError, KeyError):
                return cls()
        return cls()

    def record_primary_success(self) -> bool:
        """Record a successful primary call. Returns True if exiting fallback mode."""
        was_fallback = self.fallback_mode
        self.consecutive_failures = 0
        self.fallback_mode = False
        return was_fallback

    def record_primary_failure(self, threshold: int) -> bool:
        """Record a primary failure. Returns True if this triggers fallback mode entry."""
        self.consecutive_failures += 1
        if not self.fallback_mode and self.consecutive_failures >= threshold:
            self.fallback_mode = True
            return True
        return False

    def should_probe_primary(self, interval: int) -> bool:
        """Check if enough time has passed to try the primary provider again."""
        if not self.fallback_mode:
            return True
        return (time.monotonic() - self.last_primary_probe) >= interval

    def mark_primary_probed(self) -> None:
        """Mark the current time as the last primary probe attempt."""
        self.last_primary_probe = time.monotonic()
