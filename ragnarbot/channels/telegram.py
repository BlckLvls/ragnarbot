"""Telegram channel implementation using python-telegram-bot."""

import asyncio
import re

import telegram
from loguru import logger
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from ragnarbot.auth.grants import PendingGrantStore
from ragnarbot.bus.events import MediaAttachment, OutboundMessage
from ragnarbot.bus.queue import MessageBus
from ragnarbot.channels.base import BaseChannel
from ragnarbot.config.schema import TelegramConfig
from ragnarbot.media.manager import MediaManager
from ragnarbot.providers.transcription import TranscriptionError, TranscriptionProvider


BOT_COMMANDS = [
    ("new", "Start a new conversation"),
    ("context", "Show context usage"),
    ("context_mode", "Change context mode"),
    ("stop", "Stop agent response"),
]


async def set_bot_commands(bot, chat_ids: list[int] | None = None) -> None:
    """Ensure bot command menu is up to date across all relevant scopes.

    Checks the default scope against BOT_COMMANDS and updates only if stale.
    Clears higher-priority scopes (all_private_chats, all_group_chats) and
    per-chat overrides for known users so the default scope is authoritative.
    """
    from telegram import (
        BotCommand, BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats, BotCommandScopeChat,
    )

    target = [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS]

    def _matches(current: list) -> bool:
        return (
            len(current) == len(target)
            and all(
                a.command == b.command and a.description == b.description
                for a, b in zip(current, target)
            )
        )

    updated = False

    # 1. Ensure default scope has current commands
    if not _matches(await bot.get_my_commands()):
        await bot.set_my_commands(target)
        updated = True

    # 2. Clear higher-priority scopes that would override default
    for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats()):
        try:
            if await bot.get_my_commands(scope=scope):
                await bot.delete_my_commands(scope=scope)
                updated = True
        except Exception:
            pass

    # 3. Clear stale per-chat overrides for known users
    for cid in (chat_ids or []):
        try:
            per_chat = await bot.get_my_commands(scope=BotCommandScopeChat(chat_id=cid))
            if per_chat and not _matches(per_chat):
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=cid))
                updated = True
                logger.info(f"Cleared stale command override for chat {cid}")
        except Exception:
            pass

    if updated:
        logger.info(f"Bot commands updated: {[c[0] for c in BOT_COMMANDS]}")
    else:
        logger.debug("Bot commands already up to date")


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""
    
    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)
    
    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    
    text = re.sub(r'`([^`]+)`', save_inline_code, text)
    
    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    
    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    
    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)
    
    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    
    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")
    
    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")
    
    return text


TELEGRAM_MAX_LENGTH = 4096
_TAG_OVERHEAD = 100  # Reserve space for closing/reopening tags across splits


def _avoid_tag_split(text: str, pos: int) -> int:
    """Adjust split position to avoid splitting inside an HTML tag."""
    last_open = text.rfind('<', 0, pos)
    if last_open != -1:
        last_close = text.rfind('>', last_open, pos)
        if last_close == -1:
            return last_open
    return pos


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best position to split text, preferring natural boundaries."""
    min_pos = max_length // 4

    # Paragraph boundary
    pos = text.rfind('\n\n', 0, max_length)
    if pos > min_pos:
        return _avoid_tag_split(text, pos)

    # Line boundary
    pos = text.rfind('\n', 0, max_length)
    if pos > min_pos:
        return _avoid_tag_split(text, pos)

    # Word boundary
    pos = text.rfind(' ', 0, max_length)
    if pos > min_pos:
        return _avoid_tag_split(text, pos)

    return _avoid_tag_split(text, max_length)


def _balance_html_tags(chunk: str) -> tuple[str, str]:
    """Close unclosed HTML tags in chunk, return tags to reopen in next chunk."""
    tag_pattern = re.compile(r'<(/?)(\w+)(?:\s[^>]*)?>')
    open_tags: list[tuple[str, str]] = []  # (tag_name, full_opening_tag)

    for match in tag_pattern.finditer(chunk):
        is_closing = match.group(1) == '/'
        tag_name = match.group(2)

        if is_closing:
            for i in range(len(open_tags) - 1, -1, -1):
                if open_tags[i][0] == tag_name:
                    open_tags.pop(i)
                    break
        else:
            open_tags.append((tag_name, match.group(0)))

    if not open_tags:
        return chunk, ""

    closing = ''.join(f'</{tag}>' for tag, _ in reversed(open_tags))
    reopening = ''.join(full_tag for _, full_tag in open_tags)
    return chunk + closing, reopening


def _split_html_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split HTML text into chunks that fit Telegram's message limit."""
    if len(text) <= max_length:
        return [text]

    effective_limit = max_length - _TAG_OVERHEAD
    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, effective_limit)
        chunk = remaining[:split_at]
        remaining = remaining[split_at:].lstrip('\n')

        chunk, reopen_tags = _balance_html_tags(chunk)
        chunks.append(chunk)
        if reopen_tags:
            remaining = reopen_tags + remaining

    return chunks


def _split_plain_text(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split plain text into chunks that fit Telegram's message limit."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    min_pos = max_length // 4

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        pos = remaining.rfind('\n\n', 0, max_length)
        if pos <= min_pos:
            pos = remaining.rfind('\n', 0, max_length)
        if pos <= min_pos:
            pos = remaining.rfind(' ', 0, max_length)
        if pos <= min_pos:
            pos = max_length

        chunks.append(remaining[:pos])
        remaining = remaining[pos:].lstrip('\n')

    return chunks


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.
    
    Simple and reliable - no webhook/public IP needed.
    """
    
    name = "telegram"
    
    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        bot_token: str = "",
        transcription_provider: TranscriptionProvider | None = None,
        media_manager: MediaManager | None = None,
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.bot_token = bot_token
        self._transcriber = transcription_provider
        self._transcription_semaphore = asyncio.Semaphore(2)
        self.media_manager = media_manager
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._grants = PendingGrantStore()
    
    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.bot_token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Build the application
        self._app = (
            Application.builder()
            .token(self.bot_token)
            .build()
        )
        
        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL) 
                & ~filters.COMMAND, 
                self._on_message
            )
        )
        
        # Add command handlers
        from telegram.ext import CallbackQueryHandler, CommandHandler
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("new", self._on_new))
        self._app.add_handler(CommandHandler("stop", self._on_stop))
        self._app.add_handler(CommandHandler("context", self._on_context))
        self._app.add_handler(CommandHandler("context_mode", self._on_context_mode))
        self._app.add_handler(CallbackQueryHandler(
            self._on_callback_query, pattern="^ctx_mode:",
        ))
        
        logger.info("Starting Telegram bot (polling mode)...")
        
        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        
        # Get bot info and set commands
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")

        # Pass known chat IDs (from allow_from) to clear per-chat command overrides
        chat_ids = []
        for uid in (self.config.allow_from or []):
            try:
                chat_ids.append(int(uid))
            except (ValueError, TypeError):
                pass
        await set_bot_commands(self._app.bot, chat_ids=chat_ids or None)
        
        # Register media download callback
        if self.media_manager:
            app = self._app

            async def _download_by_file_id(file_id: str) -> tuple[bytes, str]:
                file = await app.bot.get_file(file_id)
                data = await file.download_as_bytearray()
                name = file.file_path.split("/")[-1] if file.file_path else ""
                return bytes(data), name

            self.media_manager.register_download_callback("telegram", _download_by_file_id)

        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True  # Ignore old messages on startup
        )

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False
        
        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.chat_id}")
            return

        # Typing signal — start background typing loop and return early
        if msg.metadata.get("chat_action") == "typing":
            if chat_id not in self._typing_tasks:
                self._typing_tasks[chat_id] = asyncio.create_task(
                    self._typing_loop(chat_id)
                )
            return

        # Intermediate message — send text but keep typing active
        is_intermediate = msg.metadata.get("intermediate", False)

        # Final message — stop typing first (unless keep_typing is set)
        if not is_intermediate and not msg.metadata.get("keep_typing"):
            self._stop_typing(chat_id)

        # --- Reaction handling ---
        if msg.metadata.get("reaction"):
            try:
                from telegram import ReactionTypeEmoji
                await self._app.bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=msg.metadata["target_message_id"],
                    reaction=[ReactionTypeEmoji(emoji=msg.metadata["reaction"])],
                )
            except Exception as e:
                logger.error(f"Error setting reaction: {e}")
            return

        # --- Media sending ---
        media_type = msg.metadata.get("media_type")
        if media_type and msg.metadata.get("media_path"):
            media_path = msg.metadata["media_path"]
            html_caption = _markdown_to_telegram_html(msg.content) if msg.content else None
            try:
                if media_type == "photo":
                    with open(media_path, "rb") as f:
                        await self._app.bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=html_caption,
                            parse_mode="HTML" if html_caption else None,
                        )
                elif media_type == "video":
                    with open(media_path, "rb") as f:
                        await self._app.bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=html_caption,
                            parse_mode="HTML" if html_caption else None,
                        )
                elif media_type == "document":
                    with open(media_path, "rb") as f:
                        await self._app.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            caption=html_caption,
                            parse_mode="HTML" if html_caption else None,
                        )
            except Exception as e:
                logger.error(f"Error sending {media_type}: {e}")
            return

        try:
            # Convert markdown to Telegram HTML (skip if already raw HTML)
            if msg.metadata.get("raw_html"):
                html_content = msg.content
            else:
                html_content = _markdown_to_telegram_html(msg.content)

            # Build optional reply_markup for inline keyboards
            reply_markup = None
            if msg.metadata.get("inline_keyboard"):
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                rows = []
                for row in msg.metadata["inline_keyboard"]:
                    rows.append([
                        InlineKeyboardButton(
                            text=btn["text"],
                            callback_data=btn.get("callback_data"),
                        )
                        for btn in row
                    ])
                reply_markup = InlineKeyboardMarkup(rows)

            # Edit existing message (callback response) or send new
            edit_id = msg.metadata.get("edit_message_id")
            if edit_id:
                # Edits can't become multiple messages — truncate if needed
                if len(html_content) > TELEGRAM_MAX_LENGTH:
                    html_content = html_content[:TELEGRAM_MAX_LENGTH - 3] + "..."
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_id,
                    text=html_content,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                chunks = _split_html_message(html_content)
                for i, chunk in enumerate(chunks):
                    is_last = i == len(chunks) - 1
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode="HTML",
                        reply_markup=reply_markup if is_last else None,
                    )
        except Exception as e:
            # Fallback to plain text if HTML parsing fails
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            try:
                for chunk in _split_plain_text(msg.content):
                    await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                    )
            except Exception as e2:
                logger.error(f"Error sending Telegram message: {e2}")
    
    async def _typing_loop(self, chat_id: int) -> None:
        """Send typing action every 4s until cancelled."""
        try:
            while True:
                await self._app.bot.send_chat_action(
                    chat_id=chat_id,
                    action=telegram.constants.ChatAction.TYPING,
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    def _stop_typing(self, chat_id: int) -> None:
        """Cancel the typing loop for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()

    async def _on_unauthorized(
        self, sender_id: str, chat_id: str, metadata: dict
    ) -> None:
        """Send access grant prompt to unauthorized users."""
        # Extract numeric user_id from composite sender_id (e.g. "123456|username")
        user_id = sender_id.split("|")[0]
        await self._handle_unauthorized_user(user_id, chat_id)

    async def _handle_unauthorized_user(self, user_id: str, chat_id: str) -> None:
        """Generate a grant code and send the access prompt."""
        code = self._grants.get_or_create(user_id, chat_id)
        html = (
            "<b>Access Verification</b>\n\n"
            "To grant access to this bot, run the following command in your terminal:\n\n"
            f"<code>ragnarbot telegram grant-access {code}</code>"
        )
        if self._app:
            try:
                await self._app.bot.send_message(
                    chat_id=int(chat_id),
                    text=html,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to send access prompt: {e}")

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        if not self.is_allowed(sender_id):
            await self._handle_unauthorized_user(str(user.id), str(update.message.chat_id))
            return

        await update.message.reply_text(
            f"Hi {user.first_name}! I'm ragnarbot.\n\n"
            "Send me a message and I'll respond!"
        )
    
    async def _on_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new command — create a new chat session."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        chat_id = update.message.chat_id

        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        self._chat_ids[sender_id] = chat_id

        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content="/new",
            metadata={
                "command": "new_chat",
                "message_id": update.message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            }
        )

    async def _on_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /context command — show context usage info."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        chat_id = update.message.chat_id
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        self._chat_ids[sender_id] = chat_id
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content="/context",
            metadata={
                "command": "context_info",
                "message_id": update.message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        )

    async def _on_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop command — stop the current agent response."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        chat_id = update.message.chat_id
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        self._chat_ids[sender_id] = chat_id
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content="/stop",
            metadata={
                "command": "stop",
                "message_id": update.message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        )

    async def _on_context_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /context_mode command — show mode picker."""
        if not update.message or not update.effective_user:
            return
        user = update.effective_user
        chat_id = update.message.chat_id
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        self._chat_ids[sender_id] = chat_id
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content="/context_mode",
            metadata={
                "command": "context_mode",
                "message_id": update.message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        )

    async def _on_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle callback queries from inline keyboard buttons."""
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            return
        await query.answer()

        user = update.effective_user
        chat_id = query.message.chat_id if query.message else None
        if not chat_id:
            return

        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        self._chat_ids[sender_id] = chat_id

        # ctx_mode:<mode> → set_context_mode command
        if query.data.startswith("ctx_mode:"):
            mode = query.data.split(":", 1)[1]
            await self._handle_message(
                sender_id=sender_id,
                chat_id=str(chat_id),
                content=f"/context_mode {mode}",
                metadata={
                    "command": "set_context_mode",
                    "context_mode": mode,
                    "callback_message_id": query.message.message_id if query.message else None,
                    "user_id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                },
            )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id

        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Build content from text and/or media
        content_parts: list[str] = []
        media_paths: list[str] = []
        attachments: list[MediaAttachment] = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # --- Photos: eager download into memory (no disk save yet) ---
        if message.photo and self._app:
            photo = message.photo[-1]  # Largest resolution
            try:
                file = await self._app.bot.get_file(photo.file_id)
                data = await file.download_as_bytearray()
                mime = "image/jpeg"  # Telegram photos are always JPEG
                attachments.append(MediaAttachment(
                    type="photo",
                    file_id=photo.file_id,
                    data=bytes(data),
                    mime_type=mime,
                ))
                logger.debug(f"Downloaded photo {photo.file_id[:16]} into memory")
            except Exception as e:
                logger.error(f"Failed to download photo: {e}")
                content_parts.append("[photo: download failed]")

        # --- Voice / Audio: download + transcribe ---
        elif message.voice or message.audio:
            media_file = message.voice or message.audio
            media_type = "voice" if message.voice else "audio"
            if self._app:
                text, path = await self._transcribe_voice(media_file, media_type)
                content_parts.append(text)
                if path:
                    media_paths.append(path)

        # --- Documents / files: lazy (NO download) ---
        elif message.document:
            doc = message.document
            doc_name = doc.file_name or "unnamed_file"
            attachments.append(MediaAttachment(
                type="file",
                file_id=doc.file_id,
                data=None,
                filename=doc_name,
                mime_type=doc.mime_type or "",
            ))
            content_parts.append(f"[file available: {doc_name} (file_id: {doc.file_id})]")

        content = "\n".join(content_parts) if content_parts else "[empty message]"

        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")

        # Forward to the message bus
        metadata = {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "is_group": message.chat.type != "private",
        }

        if message.reply_to_message:
            reply_data = {}
            if message.reply_to_message.from_user:
                reply_user = message.reply_to_message.from_user
                reply_data.update({
                    "user_id": reply_user.id,
                    "username": reply_user.username,
                    "first_name": reply_user.first_name,
                    "last_name": reply_user.last_name,
                })
            reply_data["content"] = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )
            if message.reply_to_message.photo and self._app:
                reply_photo = message.reply_to_message.photo[-1]
                try:
                    file = await self._app.bot.get_file(reply_photo.file_id)
                    data = await file.download_as_bytearray()
                    reply_data["photo_data"] = bytes(data)
                    reply_data["photo_mime"] = "image/jpeg"
                except Exception as e:
                    logger.error(f"Failed to download reply photo: {e}")
            reply_voice = (
                message.reply_to_message.voice or message.reply_to_message.audio
            )
            if reply_voice and self._app:
                media_type = "voice" if message.reply_to_message.voice else "audio"
                text, _ = await self._transcribe_voice(reply_voice, media_type)
                existing = reply_data.get("content", "")
                reply_data["content"] = f"{existing}\n{text}".strip()
            metadata["reply_to"] = reply_data

        if message.forward_origin:
            from telegram import MessageOriginUser
            if isinstance(message.forward_origin, MessageOriginUser):
                fwd_user = message.forward_origin.sender_user
                metadata["forwarded_from"] = {
                    "user_id": fwd_user.id,
                    "username": fwd_user.username,
                    "first_name": fwd_user.first_name,
                    "last_name": fwd_user.last_name,
                }

        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat_id),
            content=content,
            media=media_paths,
            attachments=attachments,
            metadata=metadata,
        )
    
    async def _transcribe_voice(
        self, media_file: object, media_type: str,
    ) -> tuple[str, str | None]:
        """Download and transcribe a voice/audio message.

        Returns (content_string, media_path_or_None).
        """
        from pathlib import Path

        try:
            file = await self._app.bot.get_file(media_file.file_id)
            ext = self._get_extension(media_type, getattr(media_file, "mime_type", None))

            media_dir = Path.home() / ".ragnarbot" / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
            await file.download_to_drive(str(file_path))
        except Exception as e:
            logger.error(f"Failed to download {media_type}: {e}")
            return f"[{media_type}: download failed]", None

        if not self._transcriber:
            return f"[{media_type}: {file_path}]", str(file_path)

        try:
            async with self._transcription_semaphore:
                text = await self._transcriber.transcribe(file_path)
            logger.info(f"Transcribed {media_type}: {text[:50]}...")
            return f"[Voice message transcription: {text}]", str(file_path)
        except TranscriptionError as e:
            logger.error(f"Transcription failed: {e.detail or e.short_message}")
            return (
                f"[Voice message — transcription failed: {e.short_message}]",
                str(file_path),
            )

    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]
        
        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
