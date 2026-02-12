"""Tests for the restart tool."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ragnarbot.agent.tools.restart import RESTART_MARKER, RestartTool


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent._restart_requested = False

    def set_restart():
        agent._restart_requested = True

    agent.request_restart = MagicMock(side_effect=set_restart)
    return agent


@pytest.fixture
def restart_tool(mock_agent):
    return RestartTool(agent=mock_agent)


@pytest.mark.asyncio
async def test_execute_sets_restart_flag(restart_tool, mock_agent):
    result = await restart_tool.execute()
    data = json.loads(result)
    assert data["status"] == "restart_scheduled"
    mock_agent.request_restart.assert_called_once()
    assert mock_agent._restart_requested is True


@pytest.mark.asyncio
async def test_execute_returns_json(restart_tool):
    result = await restart_tool.execute()
    data = json.loads(result)
    assert "status" in data
    assert "note" in data


@pytest.mark.asyncio
async def test_execute_writes_marker_with_context(restart_tool, tmp_path):
    marker = tmp_path / ".restart_marker"
    with patch("ragnarbot.agent.tools.restart.RESTART_MARKER", marker):
        restart_tool.set_context("telegram", "12345")
        await restart_tool.execute()

    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["channel"] == "telegram"
    assert data["chat_id"] == "12345"
    marker.unlink()


@pytest.mark.asyncio
async def test_execute_no_marker_without_context(restart_tool, tmp_path):
    marker = tmp_path / ".restart_marker"
    with patch("ragnarbot.agent.tools.restart.RESTART_MARKER", marker):
        # No set_context called — channel/chat_id are empty
        await restart_tool.execute()
    assert not marker.exists()


def test_set_context(restart_tool):
    restart_tool.set_context("telegram", "99999")
    assert restart_tool._channel == "telegram"
    assert restart_tool._chat_id == "99999"


def test_tool_metadata(restart_tool):
    assert restart_tool.name == "restart"
    assert "restart" in restart_tool.description.lower()
    assert restart_tool.parameters == {"type": "object", "properties": {}}
