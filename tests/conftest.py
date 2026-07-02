"""Shared pytest fixtures."""

import pytest

from ragnarbot.agent.tools import ripgrep


@pytest.fixture(autouse=True)
def _isolated_profile_root(tmp_path, monkeypatch):
    """Keep every test out of the real ~/.ragnarbot profile.

    All profile paths (sessions, markers, cron, hooks, state) resolve from
    Path.home() inside ragnarbot.instance; without this override, AgentLoop
    and SessionManager tests write their sessions into the developer's live
    profile — they then show up as junk chats in Telegram/web history.
    Tests that assert on path layout patch Path.home themselves and override
    this default within their own scope.
    """
    fake_home = tmp_path / "profile-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("ragnarbot.instance.Path.home", lambda: fake_home)


@pytest.fixture(autouse=True)
def _block_ripgrep_download(monkeypatch):
    """Never download ripgrep over the network during tests.

    Provisioning logic is unit-tested with mocks in test_ripgrep_provision.py;
    everywhere else, a blocked download just degrades to the Python search
    fallback (ensure_ripgrep swallows the error and returns None), so no test
    hits the network or writes a binary into the real profile data root.
    """
    async def _blocked(asset, dest):
        raise RuntimeError("ripgrep download disabled during tests")

    monkeypatch.setattr(ripgrep, "_download_and_extract", _blocked)
