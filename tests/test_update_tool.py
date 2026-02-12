"""Tests for the update tool."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ragnarbot.agent.tools.update import UpdateTool, _parse_version

# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------

def test_parse_version():
    assert _parse_version("v0.3.0") == (0, 3, 0)
    assert _parse_version("0.3.0") == (0, 3, 0)
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("v0.4.0") > _parse_version("v0.3.0")
    assert _parse_version("v0.3.0") == _parse_version("0.3.0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent():
    a = MagicMock()
    a.request_restart = MagicMock()
    return a


@pytest.fixture
def tool(agent):
    t = UpdateTool(agent=agent)
    t.set_context("telegram", "12345")
    return t


def _make_tags_response(tags: list[str]) -> httpx.Response:
    """Build a fake httpx.Response for the tags endpoint."""
    data = [{"name": t} for t in tags]
    return httpx.Response(200, json=data, request=httpx.Request("GET", "https://x"))


def _make_release_response(
    version: str, body: str, name: str | None = None,
) -> httpx.Response:
    """Build a fake httpx.Response for the releases/tags endpoint."""
    data = {
        "name": name or f"v{version}",
        "body": body,
        "html_url": f"https://github.com/BlckLvls/ragnarbot/releases/tag/v{version}",
        "published_at": "2025-01-15T12:00:00Z",
    }
    return httpx.Response(200, json=data, request=httpx.Request("GET", "https://x"))


# ---------------------------------------------------------------------------
# check action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_update_available(tool):
    with patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg:
        mock_pkg.__version__ = "0.3.0"
        with patch("httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get = AsyncMock(return_value=_make_tags_response(["v0.2.0", "v0.4.0", "v0.3.0"]))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = json.loads(await tool.execute(action="check"))

    assert result["current_version"] == "0.3.0"
    assert result["latest_version"] == "0.4.0"
    assert result["update_available"] is True


@pytest.mark.asyncio
async def test_check_already_latest(tool):
    with patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg:
        mock_pkg.__version__ = "0.4.0"
        with patch("httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get = AsyncMock(return_value=_make_tags_response(["v0.3.0", "v0.4.0"]))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = json.loads(await tool.execute(action="check"))

    assert result["current_version"] == "0.4.0"
    assert result["latest_version"] == "0.4.0"
    assert result["update_available"] is False


@pytest.mark.asyncio
async def test_check_github_error(tool):
    with patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg:
        mock_pkg.__version__ = "0.3.0"
        with patch("httpx.AsyncClient") as mock_client_cls:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("connection failed"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = json.loads(await tool.execute(action="check"))

    assert "error" in result


# ---------------------------------------------------------------------------
# changelog action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_changelog_returns_release_notes(tool):
    release_body = "## What's new\n- Added update tool\n- Fixed edge case"
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_release_response("0.4.0", release_body)
        )
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = json.loads(await tool.execute(action="changelog", version="0.4.0"))

    assert result["version"] == "0.4.0"
    assert result["body"] == release_body
    assert "url" in result


@pytest.mark.asyncio
async def test_changelog_default_version_uses_latest(tool):
    """When version is omitted, should fetch latest and use it as target."""
    tags_resp = _make_tags_response(["v0.3.0", "v0.5.0"])
    release_resp = _make_release_response("0.5.0", "Release notes for 0.5.0")

    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        # First call: tags (from _get_latest_version), second call: release
        client.get = AsyncMock(side_effect=[tags_resp, release_resp])
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = json.loads(await tool.execute(action="changelog"))

    assert result["version"] == "0.5.0"
    assert result["body"] == "Release notes for 0.5.0"


@pytest.mark.asyncio
async def test_changelog_same_version_shows_release(tool):
    """Asking for changelog of current version should still return release notes."""
    release_body = "Initial release with core features"
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_make_release_response("0.3.0", release_body)
        )
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = json.loads(await tool.execute(action="changelog", version="0.3.0"))

    assert result["version"] == "0.3.0"
    assert result["body"] == release_body


# ---------------------------------------------------------------------------
# update action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_runs_upgrade_and_restarts(tool, agent, tmp_path):
    marker_path = tmp_path / ".update_marker"

    with (
        patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg,
        patch("ragnarbot.agent.tools.update.UPDATE_MARKER", marker_path),
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        mock_pkg.__version__ = "0.3.0"

        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_tags_response(["v0.3.0", "v0.4.0"]))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_exec.return_value = proc

        result = json.loads(await tool.execute(action="update"))

    assert result["status"] == "updating"
    assert result["old_version"] == "0.3.0"
    assert result["new_version"] == "0.4.0"
    agent.request_restart.assert_called_once()
    assert marker_path.exists()
    marker_data = json.loads(marker_path.read_text())
    assert marker_data["old_version"] == "0.3.0"
    assert marker_data["new_version"] == "0.4.0"


@pytest.mark.asyncio
async def test_update_pip_fallback(tool, agent, tmp_path):
    marker_path = tmp_path / ".update_marker"

    with (
        patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg,
        patch("ragnarbot.agent.tools.update.UPDATE_MARKER", marker_path),
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        mock_pkg.__version__ = "0.3.0"

        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_tags_response(["v0.3.0", "v0.4.0"]))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # uv raises FileNotFoundError, pip succeeds
        pip_proc = AsyncMock()
        pip_proc.returncode = 0
        pip_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_exec.side_effect = [FileNotFoundError("uv not found"), pip_proc]

        result = json.loads(await tool.execute(action="update"))

    assert result["status"] == "updating"
    agent.request_restart.assert_called_once()
    # Second call should be pip
    assert mock_exec.call_count == 2
    second_call_args = mock_exec.call_args_list[1][0]
    assert second_call_args[0] == "pip"


@pytest.mark.asyncio
async def test_update_already_latest(tool, agent):
    with (
        patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg,
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        mock_pkg.__version__ = "0.4.0"

        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_tags_response(["v0.3.0", "v0.4.0"]))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = json.loads(await tool.execute(action="update"))

    assert result["status"] == "up_to_date"
    mock_exec.assert_not_called()
    agent.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_update_both_fail(tool, agent):
    with (
        patch("ragnarbot.agent.tools.update.ragnarbot") as mock_pkg,
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("asyncio.create_subprocess_exec") as mock_exec,
    ):
        mock_pkg.__version__ = "0.3.0"

        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_tags_response(["v0.3.0", "v0.4.0"]))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # uv not found, pip also not found
        mock_exec.side_effect = FileNotFoundError("not found")

        result = json.loads(await tool.execute(action="update"))

    assert "error" in result
    assert "Neither uv nor pip" in result["error"]
    agent.request_restart.assert_not_called()
