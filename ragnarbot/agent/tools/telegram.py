"""Telegram media and reaction tools."""

from typing import Any, Awaitable, Callable

from telegram.constants import ReactionEmoji

from ragnarbot.agent.tools.base import Tool
from ragnarbot.bus.events import OutboundMessage

VALID_TELEGRAM_REACTIONS = frozenset(e.value for e in ReactionEmoji)


class SendPhotoTool(Tool):
    """Tool to send a photo to the user on Telegram."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._send_callback = send_callback
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "send_photo"

    @property
    def description(self) -> str:
        return (
            "Send a photo to the user. Telegram will compress the image for quick viewing. "
            "Use send_file instead if the user wants original quality."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the image file",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the photo (supports markdown)",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str, caption: str = "", **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=caption,
            metadata={"media_type": "photo", "media_path": file_path, "keep_typing": True},
        )
        try:
            await self._send_callback(msg)
            return f"Photo sent: {file_path}"
        except Exception as e:
            return f"Error sending photo: {e}"


class SendVideoTool(Tool):
    """Tool to send a video to the user on Telegram."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._send_callback = send_callback
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "send_video"

    @property
    def description(self) -> str:
        return (
            "Send a video to the user. Telegram will compress the video for quick viewing. "
            "Use send_file instead if the user wants original quality."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the video file",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the video (supports markdown)",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str, caption: str = "", **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=caption,
            metadata={"media_type": "video", "media_path": file_path, "keep_typing": True},
        )
        try:
            await self._send_callback(msg)
            return f"Video sent: {file_path}"
        except Exception as e:
            return f"Error sending video: {e}"


class SendFileTool(Tool):
    """Tool to send a file/document to the user on Telegram."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._send_callback = send_callback
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "send_file"

    @property
    def description(self) -> str:
        return (
            "Send a file as a Telegram document (original quality, no compression). "
            "Use for non-media files, or when the user wants uncompressed originals."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption for the file (supports markdown)",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str, caption: str = "", **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=caption,
            metadata={"media_type": "document", "media_path": file_path, "keep_typing": True},
        )
        try:
            await self._send_callback(msg)
            return f"File sent: {file_path}"
        except Exception as e:
            return f"Error sending file: {e}"


class SetReactionTool(Tool):
    """Tool to react to the user's last message with an emoji."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    ):
        self._send_callback = send_callback
        self._channel = ""
        self._chat_id = ""
        self._message_id: int | None = None

    def set_context(self, channel: str, chat_id: str, message_id: int | None = None) -> None:
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id

    @property
    def name(self) -> str:
        return "set_reaction"

    @property
    def description(self) -> str:
        return (
            "React to the user's last message with a single emoji. "
            "The target message is set automatically — just provide the emoji."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "A single emoji character to react with. Must be a valid Telegram reaction emoji.",
                },
            },
            "required": ["emoji"],
        }

    async def execute(self, emoji: str, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Message sending not configured"
        if not self._message_id:
            return "Error: No target message to react to"

        emoji = emoji.strip()
        if emoji not in VALID_TELEGRAM_REACTIONS:
            valid_list = " ".join(sorted(VALID_TELEGRAM_REACTIONS))
            return (
                f"Error: '{emoji}' is not a valid Telegram reaction. "
                f"Valid reactions: {valid_list}"
            )

        msg = OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content="",
            metadata={"reaction": emoji, "target_message_id": self._message_id},
        )
        try:
            await self._send_callback(msg)
            return f"Reaction {emoji} set"
        except Exception as e:
            return f"Error setting reaction: {e}"
