"""Tests for profile-local browser storage."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import ragnarbot.agent.tools.browser as browser_module
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


def _manager_with_missing_chromium(tmp_path):
    instance = SimpleNamespace(
        browser_profile_path=tmp_path / "browser-profile",
        browser_screenshots_path=tmp_path / "browser-screenshots",
    )
    with patch("ragnarbot.agent.tools.browser.ensure_instance_root", return_value=instance):
        manager = BrowserSessionManager(config=_config())
    manager._playwright = SimpleNamespace(
        chromium=SimpleNamespace(executable_path=str(tmp_path / "missing-chromium")),
    )
    return manager


@pytest.mark.asyncio
async def test_chromium_installer_drains_output_with_communicate(tmp_path):
    manager = _manager_with_missing_chromium(tmp_path)
    proc = SimpleNamespace(
        pid=1234,
        returncode=0,
        communicate=AsyncMock(return_value=(b"installed", b"")),
    )

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        await manager._install_chromium_if_needed()

    proc.communicate.assert_awaited_once()
    assert manager._chromium_installed is True


@pytest.mark.asyncio
async def test_chromium_installer_timeout_cleans_process_tree(tmp_path, monkeypatch):
    manager = _manager_with_missing_chromium(tmp_path)

    async def _hang():
        await asyncio.Event().wait()

    proc = SimpleNamespace(
        pid=1234,
        returncode=None,
        communicate=AsyncMock(side_effect=_hang),
    )
    terminate = AsyncMock()
    monkeypatch.setattr(browser_module, "BROWSER_INSTALL_TIMEOUT_SECONDS", 0.01)

    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("ragnarbot.agent.tools.browser.terminate_process_tree", terminate),
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            await manager._install_chromium_if_needed()

    terminate.assert_awaited_once_with(proc)
    assert manager._chromium_installed is False
