"""Event types for the message bus."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MediaAttachment:
    """A media attachment on an inbound message."""

    type: str  # "photo" | "file" | "voice" | "audio"
    file_id: str  # Platform file identifier (e.g. Telegram file_id)
    data: bytes | None = None  # Raw bytes (photos only — downloaded eagerly)
    filename: str = ""  # Cosmetic filename (optional)
    mime_type: str = ""


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # e.g. "telegram"
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    media: list[str] = field(default_factory=list)  # Media URLs (voice/audio)
    attachments: list[MediaAttachment] = field(default_factory=list)  # Structured media
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    
    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        override = self.metadata.get("session_key_override")
        if override:
            return override
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""
    
    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


