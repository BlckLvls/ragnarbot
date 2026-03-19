"""Tests for profile-local browser storage."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ragnarbot.agent.tools.browser import BrowserSession, BrowserSessionManager


def _config():
    return SimpleNamespace(
        idle_timeout=60,
        headless=True,
        viewport_width=1280,
        viewport_height=720,
    )


@pytest.mark.asyncio
async def test_open_uses_profile_local_browser_dir(tmp_path):
    instance = SimpleNamespace(
        browser_profile_path=tmp_path / "browser-profile",
        browser_screenshots_path=tmp_path / "browser-screenshots",
    )
    fake_page = AsyncMock()
    fake_page.title = AsyncMock(return_value="")
    fake_page.url = "about:blank"
    fake_context = AsyncMock()
    fake_context.pages = []
    fake_context.new_page = AsyncMock(return_value=fake_page)
    fake_pw = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=AsyncMock(return_value=fake_context),
        ),
    )

    with patch("ragnarbot.agent.tools.browser.ensure_instance_root", return_value=instance):
        manager = BrowserSessionManager(config=_config())

    manager._ensure_playwright = AsyncMock(return_value=fake_pw)
    manager._reset_idle_timer = lambda session: None

    await manager.open()

    kwargs = fake_pw.chromium.launch_persistent_context.await_args.kwargs
    assert kwargs["user_data_dir"] == str(instance.browser_profile_path)


@pytest.mark.asyncio
async def test_screenshot_uses_profile_local_screenshot_dir(tmp_path):
    instance = SimpleNamespace(
        browser_profile_path=tmp_path / "browser-profile",
        browser_screenshots_path=tmp_path / "browser-screenshots",
    )
    fake_page = AsyncMock()
    fake_page.screenshot = AsyncMock(return_value=b"png-bytes")
    fake_page.title = AsyncMock(return_value="Example")
    fake_page.url = "https://example.com"

    with patch("ragnarbot.agent.tools.browser.ensure_instance_root", return_value=instance):
        manager = BrowserSessionManager(config=_config())

    manager._reset_idle_timer = lambda session: None
    session = BrowserSession(
        session_id="abc12345",
        context=AsyncMock(),
        page=fake_page,
        persistent=True,
        created_at=time.time(),
        last_activity=time.time(),
    )
    manager._sessions[session.session_id] = session

    result = await manager.screenshot(session.session_id)

    saved_files = list(instance.browser_screenshots_path.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].name.startswith("abc12345_")
    assert str(instance.browser_screenshots_path) in result[1]["text"]
