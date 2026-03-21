"""Tests for Telegram channel edit and callback behavior."""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import telegram

from ragnarbot.bus.events import OutboundMessage
from ragnarbot.channels.telegram import CALLBACK_QUERY_PATTERN, TelegramChannel
from ragnarbot.config.schema import TelegramConfig


def _make_channel() -> tuple[TelegramChannel, MagicMock]:
    """Create a TelegramChannel with a mocked bot app."""
    channel = TelegramChannel(
        config=TelegramConfig(enabled=True),
        bus=MagicMock(),
        bot_token="test-token",
    )
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    app = MagicMock()
    app.bot = bot
    channel._app = app
    return channel, bot


def test_callback_query_pattern_matches_install_and_toggle_callbacks():
    """Inline callback routing should include install and toggle actions."""
    assert re.match(CALLBACK_QUERY_PATTERN, "lightning_mode:on")
    assert re.match(CALLBACK_QUERY_PATTERN, "install_codex_cli")
    assert not re.match(CALLBACK_QUERY_PATTERN, "unknown_action")


@pytest.mark.asyncio
async def test_send_ignores_not_modified_edit_without_plain_text_fallback():
    """Unchanged edits should be treated as no-ops, not duplicated as new messages."""
    channel, bot = _make_channel()
    bot.edit_message_text.side_effect = telegram.error.BadRequest(
        "Message is not modified: specified new message content and reply markup are exactly the same",
    )

    await channel.send(OutboundMessage(
        channel="telegram",
        chat_id="123",
        content="⚡ <b>Lightning Mode</b>",
        metadata={
            "raw_html": True,
            "edit_message_id": 42,
            "inline_keyboard": [[{"text": "Enable", "callback_data": "lightning_mode:on"}]],
        },
    ))

    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_edit_instead_of_new_message():
    """Failed HTML edits should retry as plain-text edits, not send duplicates."""
    channel, bot = _make_channel()
    bot.edit_message_text.side_effect = [
        telegram.error.BadRequest("Can't parse entities: unsupported start tag"),
        None,
    ]

    await channel.send(OutboundMessage(
        channel="telegram",
        chat_id="123",
        content="⚡ <b>Lightning Mode</b>",
        metadata={
            "raw_html": True,
            "edit_message_id": 42,
            "inline_keyboard": [[{"text": "Enable", "callback_data": "lightning_mode:on"}]],
        },
    ))

    assert bot.edit_message_text.await_count == 2
    first_kwargs = bot.edit_message_text.await_args_list[0].kwargs
    second_kwargs = bot.edit_message_text.await_args_list[1].kwargs
    assert first_kwargs["parse_mode"] == "HTML"
    assert "parse_mode" not in second_kwargs
    bot.send_message.assert_not_awaited()
