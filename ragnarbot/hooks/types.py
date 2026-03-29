"""Hook types."""

from dataclasses import dataclass, field


@dataclass
class HookDefinition:
    """A registered webhook hook."""
    id: str                    # cryptographic token — doubles as auth secret
    name: str                  # human-readable name
    instructions: str          # prompt for the isolated handler session
    mode: str = "alert"        # "alert" | "silent"
    enabled: bool = True
    channel: str | None = None  # delivery channel (e.g. "telegram")
    to: str | None = None       # delivery chat_id
    created_at_ms: int = 0
    updated_at_ms: int = 0
    trigger_count: int = 0


@dataclass
class HookStore:
    """Persistent store for registered hooks."""
    version: int = 1
    hooks: list[HookDefinition] = field(default_factory=list)
