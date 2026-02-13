"""Tests for SetReactionTool emoji validation."""

import pytest

from ragnarbot.agent.tools.telegram import VALID_TELEGRAM_REACTIONS, SetReactionTool


@pytest.fixture
def tool():
    """Create a SetReactionTool with a mock callback."""
    async def noop_callback(msg):
        pass

    t = SetReactionTool(send_callback=noop_callback)
    t.set_context(channel="telegram", chat_id="123", message_id=1)
    return t


@pytest.mark.asyncio
async def test_valid_emoji_succeeds(tool):
    result = await tool.execute(emoji="👍")
    assert result == "Reaction 👍 set"


@pytest.mark.asyncio
async def test_invalid_emoji_returns_error(tool):
    result = await tool.execute(emoji="🎤")
    assert "not a valid Telegram reaction" in result


@pytest.mark.asyncio
async def test_empty_emoji_returns_error(tool):
    result = await tool.execute(emoji="")
    assert "not a valid Telegram reaction" in result


@pytest.mark.asyncio
async def test_whitespace_stripped_valid(tool):
    result = await tool.execute(emoji=" 👍 ")
    assert result == "Reaction 👍 set"


@pytest.mark.asyncio
async def test_whitespace_stripped_invalid(tool):
    result = await tool.execute(emoji="  🎤  ")
    assert "not a valid Telegram reaction" in result


def test_valid_reactions_set_is_populated():
    assert len(VALID_TELEGRAM_REACTIONS) > 0
    assert "👍" in VALID_TELEGRAM_REACTIONS
